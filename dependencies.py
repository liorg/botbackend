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


def _verify_service_token(authorization: str | None) -> None:
    """Internal mode: the caller must present the service role key itself."""
    token = _extract_bearer_token(authorization)

    if not hmac.compare_digest(token, _get_service_key()):
        raise HTTPException(
            status_code=401,
            detail="Invalid service token",
        )


def get_supabase(
    authorization: str | None = Header(None),
) -> Client:
    # INTERNAL -> service role, but the caller still has to prove it holds it
    if APP_MODE == "internal":
        _verify_service_token(authorization)
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
        _verify_service_token(authorization)

        return {
            "uid": "internal",
            "sub": "internal",
            "email": None,
            "internal": True,
        }

    token = _extract_bearer_token(authorization)

    # Config errors must surface as 500, not 401 -> resolve before the try.
    db = _auth_client()

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
            detail="User not found for token",
        )

    user = user_response.user

    return {
        "uid": str(user.id),
        "sub": str(user.id),
        "email": user.email,
    }
