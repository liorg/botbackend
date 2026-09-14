from functools import lru_cache
import os

import jwt
from fastapi import Header, HTTPException
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
# Both modes authenticate the browser's Supabase JWT. They differ only in
# which Supabase credentials the resulting queries run under:
#   internal -> service role, RLS bypassed
#   client   -> the user's own token, RLS enforced
# ---------------------------------------------------------------------------

def get_current_user(
    authorization: str | None = Header(None),
) -> dict:
    return _verify_user_token(_extract_bearer_token(authorization))

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
    authorization: str | None = Header(None),
) -> Client:
    token = _extract_bearer_token(authorization)

    # Authenticate in both modes: the service client must never be handed
    # out to an unauthenticated caller.
    _verify_user_token(token)

    if APP_MODE == "internal":
        return _service_client()

    try:
        return _user_client(token)
    except RuntimeError as e:
        print(f"[AUTH] {e}")
        raise HTTPException(
            status_code=503,
            detail="Supabase client not configured",
        )
