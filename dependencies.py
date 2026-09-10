
from fastapi import Header, HTTPException
from supabase import Client, create_client
from supabase.client import ClientOptions
import jwt
import os

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


def get_supabase(
    authorization: str | None = Header(None),
) -> Client:
    url = _get_supabase_url()

    # INTERNAL → service role
    if APP_MODE == "internal":
        key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_SERVICE_KEY")
        )

        if not key:
            raise RuntimeError(
                "SUPABASE_SERVICE_ROLE_KEY is required in internal mode"
            )

        return create_client(url, key)

    # CLIENT → JWT של המשתמש + publishable/anon key
    token = _extract_bearer_token(authorization)
    key = _get_client_key()

    return create_client(
        url,
        key,
        options=ClientOptions(
            headers={
                "Authorization": f"Bearer {token}",
            },
            persist_session=False,
            auto_refresh_token=False,
        ),
    )


def get_current_user(
    authorization: str | None = Header(None),
):
    token = _extract_bearer_token(authorization)

    # CLIENT / Supabase access token
    try:
        url = _get_supabase_url()
        key = _get_client_key()

        db = create_client(url, key)

        user_response = db.auth.get_user(token)

        if user_response and user_response.user:
            user = user_response.user

            return {
                "uid": str(user.id),
                "sub": str(user.id),
                "email": user.email,
            }

        raise HTTPException(
            status_code=401,
            detail="User not found for token",
        )

    except HTTPException:
        raise

    except Exception as e:
        # חשוב: לא לבלוע את השגיאה
        print(
            f"[AUTH] Supabase get_user failed: "
            f"{type(e).__name__}:
