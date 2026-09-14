from functools import lru_cache
import hmac
import os

from fastapi import Header, HTTPException
from supabase import Client, create_client
from supabase.client import ClientOptions

from config import APP_MODE


def _get_supabase_url() -> str:
    url = os.getenv("SUPABASE_URL")

    if not url:
        raise RuntimeError("SUPABASE_URL is required")

    return url


def _get_client_key() -> str:
    key = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
    )

    if not key:
        raise RuntimeError(
            "SUPABASE_PUBLISHABLE_KEY or SUPABASE_ANON_KEY is required"
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


# ---------------------------------------------------------------------------
# Cached clients: one service client, one anon client for token verification.
# Creating a client per request means a new httpx pool per request.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _service_client() -> Client:
    return create_client(_get_supabase_url(), _get_service_key())


@lru_cache(maxsize=1)
def _auth_client() -> Client:
    return create_client(
        _get_supabase_url(),
        _get_client_key(),
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
        ),
    )


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


def _is_service_token(token: str) -> bool:
    """True if the caller presented the service role key itself."""
    try:
        return hmac.compare_digest(
            token.encode("utf-8"),
            _get_service_key().encode("utf-8"),
        )
    except RuntimeError:
        # Service key not configured -> fall back to user JWT verification.
        return False


def _verify_user_token(token: str) -> dict:
    try:
        db = _auth_client()
    except RuntimeError as e:
        # Missing anon/publishable key: must not escape as an unhandled 500,
        # that response bypasses CORSMiddleware and shows up as a CORS error.
        print(f"[AUTH] Auth client unavailable: {e}")
        raise HTTPException(
            status_code=503,
            detail="Auth client not configured (SUPABASE_ANON_KEY missing)",
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
    }


def _authenticate_internal(authorization: str | None) -> dict:
    """Internal mode: accept the service key, or a valid Supabase user JWT."""
    token = _extract_bearer_token(authorization)

    if _is_service_token(token):
        return {
            "uid": "service",
            "sub": "service",
            "email": None,
            "internal": True,
        }

    return _verify_user_token(token)


def get_supabase(
    authorization: str | None = Header(None),
) -> Client:
    # INTERNAL -> authenticate the caller, then hand back the service client
    if APP_MODE == "internal":
        _authenticate_internal(authorization)
        return _service_client()

    # CLIENT -> user JWT applied to PostgREST so RLS sees auth.uid()
    token = _extract_bearer_token(authorization)

    db = create_client(
        _get_supabase_url(),
        _get_client_key(),
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
        ),
    )

    # Do NOT pass Authorization via ClientOptions.headers:
    # create_client overwrites it with the anon key and RLS runs as anon.
    db.postgrest.auth(token)

    return db


def get_current_user(
    authorization: str | None = Header(None),
) -> dict:
    if APP_MODE == "internal":
        return _authenticate_internal(authorization)

    return _verify_user_token(_extract_bearer_token(authorization))
