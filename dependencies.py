from dataclasses import dataclass
from functools import lru_cache
import os

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import Client, create_client
from supabase.client import ClientOptions

from config import APP_MODE


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _get_supabase_url() -> str:
    url = os.getenv("SUPABASE_URL")

    if not url:
        raise RuntimeError("SUPABASE_URL is required")

    return url


def _get_client_key() -> str:
    """anon / publishable key. Required in client mode."""
    key = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_KEY")
    )

    if not key:
        raise RuntimeError(
            "SUPABASE_ANON_KEY (or SUPABASE_KEY) is required in client mode"
        )

    return key


def _get_service_key() -> str:
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
    )

    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is required in internal mode"
        )

    return key


def _get_jwt_secret() -> str | None:
    return os.getenv("SUPABASE_JWT_SECRET") or None


# ---------------------------------------------------------------------------
# Cached clients. A client per request means a new httpx pool per request.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _service_client() -> Client:
    """Service role: bypasses RLS. Never leaves the backend."""
    return create_client(_get_supabase_url(), _get_service_key())


@lru_cache(maxsize=1)
def _auth_client() -> Client:
    """Verify JWTs over the network. The key here is only the apikey header;
    the user's token is what actually gets verified, so in internal mode the
    service key works when no anon key is deployed."""
    try:
        key = _get_client_key()
    except RuntimeError:
        if APP_MODE != "internal":
            raise
        key = _get_service_key()

    return create_client(
        _get_supabase_url(),
        key,
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
        ),
    )


def get_service_supabase() -> Client:
    """Public accessor for the cached service-role client.

    For callers that run before a bearer token exists (login, signup, OAuth
    exchange, password reset). Never hand this client to an authenticated
    request path: use get_supabase, which honours APP_MODE and RLS.
    """
    return _service_client()


def new_auth_flow_client() -> Client:
    """Fresh client per auth flow (sign-in, sign-up, OAuth, password reset).

    Never cached and never used for table queries: signing in switches the
    client's postgrest Authorization to the user's token, so a shared client
    would run every later service-role query as that user.
    """
    return create_client(
        _get_supabase_url(),
        _get_service_key(),
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
        ),
    )


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header",
        )

    token = authorization.removeprefix("Bearer ").strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing token",
        )

    return token


def _verify_locally(token: str, secret: str) -> dict:
    """Verify the Supabase access token signature without a network call."""
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        print(f"[AUTH] Local JWT verify failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")

    uid = claims.get("sub")

    if not uid:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    return {
        "uid": str(uid),
        "sub": str(uid),
        "email": claims.get("email"),
        "role": claims.get("role"),
        "app_metadata": claims.get("app_metadata") or {},
        "user_metadata": claims.get("user_metadata") or {},
    }


def _verify_remotely(token: str) -> dict:
    """Fallback: ask Supabase to resolve the token."""
    try:
        db = _auth_client()
    except RuntimeError as e:
        # Never let a config error escape as an unhandled 500: that response
        # is built outside CORSMiddleware and surfaces as a CORS error.
        print(f"[AUTH] {e}")
        raise HTTPException(
            status_code=503,
            detail="Auth client not configured",
        )

    try:
        user_response = db.auth.get_user(token)
    except Exception as e:
        print(f"[AUTH] Supabase get_user failed: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Auth backend unavailable",
        )

    if not user_response or not user_response.user:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )

    user = user_response.user

    return {
        "uid": str(user.id),
        "sub": str(user.id),
        "email": user.email,
        "role": getattr(user, "role", None),
        "app_metadata": getattr(user, "app_metadata", None) or {},
        "user_metadata": getattr(user, "user_metadata", None) or {},
    }


def _token_alg(token: str) -> str | None:
    try:
        return jwt.get_unverified_header(token).get("alg")
    except jwt.InvalidTokenError:
        return None


def _verify_user_token(token: str) -> dict:
    secret = _get_jwt_secret()

    # Supabase projects on the new signing keys issue ES256/RS256 tokens.
    # The shared JWT secret cannot verify those, so only take the local
    # path when the token really is HS256.
    if secret and _token_alg(token) == "HS256":
        return _verify_locally(token, secret)

    return _verify_remotely(token)


# ---------------------------------------------------------------------------
# FastAPI dependencies
#
# get_auth verifies the token once per request. FastAPI caches a dependency
# per request, so an endpoint that takes both get_current_user and
# get_supabase shares one verification (one network call at most on
# ES256/RS256).
#
# Both modes authenticate the browser's Supabase JWT. They differ only in
# which Supabase credentials the resulting queries run under:
#   internal -> service role, RLS bypassed
#   client   -> the user's own token, RLS enforced
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthContext:
    token: str
    user: dict


# Declare the existing Supabase access-token authentication in OpenAPI.
# auto_error=False preserves our existing 401 error handling for missing tokens.
supabase_bearer = HTTPBearer(
    scheme_name="SupabaseJWT",
    description="Supabase Auth access token",
    auto_error=False,
)


def get_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(supabase_bearer),
) -> AuthContext:
    authorization = (
        f"Bearer {credentials.credentials}"
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else None
    )
    token = _extract_bearer_token(authorization)
    return AuthContext(token=token, user=_verify_user_token(token))


def get_current_user(
    auth: AuthContext = Depends(get_auth),
) -> dict:
    return auth.user


@lru_cache(maxsize=256)
def _user_client(token: str) -> Client:
    """CLIENT: one cached client per access token.

    Never share a single client and re-auth it per request — concurrent
    requests would overwrite each other's Authorization header and cross
    tenants. Keyed by token, so each caller keeps its own pool; Supabase
    tokens rotate roughly hourly and stale entries fall out by LRU.
    """
    db = create_client(
        _get_supabase_url(),
        _get_client_key(),
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
        ),
    )
    # Do NOT pass Authorization via ClientOptions.headers: create_client
    # overwrites it with the apikey and every query then runs as anon.
    db.postgrest.auth(token)
    return db


def get_supabase(
    auth: AuthContext = Depends(get_auth),
) -> Client:
    # Authenticated by get_auth: the service client is never handed out
    # to an unauthenticated caller.
    if APP_MODE == "internal":
        return _service_client()

    try:
        return _user_client(auth.token)
    except RuntimeError as e:
        print(f"[AUTH] {e}")
        raise HTTPException(
            status_code=503,
            detail="Supabase client not configured",
        )