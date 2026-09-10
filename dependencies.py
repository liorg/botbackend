from fastapi import Header, HTTPException
from supabase import Client, create_client
from supabase.client import ClientOptions
import jwt
import os


APP_MODE = os.getenv("APP_MODE", "client").lower()


def get_supabase(
    authorization: str | None = Header(None),
) -> Client:
    url = os.getenv("SUPABASE_URL")

    if not url:
        raise RuntimeError("SUPABASE_URL is required")

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

    # CLIENT → JWT של המשתמש + anon/publishable key
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

    key = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
    )

    if not key:
        raise RuntimeError(
            "SUPABASE_PUBLISHABLE_KEY or SUPABASE_ANON_KEY is required"
        )

    return create_client(
        url,
        key,
        options=ClientOptions(
            headers={
                "Authorization": f"Bearer {token}"
            },
            persist_session=False,
            auto_refresh_token=False,
        ),
    )


def get_current_user(
    authorization: str = Header(...),
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing authorization header",
        )

    token = authorization.removeprefix("Bearer ").strip()

    # Supabase access token
    try:
        url = os.getenv("SUPABASE_URL")
        key = (
            os.getenv("SUPABASE_PUBLISHABLE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
        )

        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and publishable/anon key are required"
            )

        db = create_client(url, key)

        user_response = db.auth.get_user(token)

        if user_response and user_response.user:
            user = user_response.user

            return {
                "uid": user.id,
                "sub": user.id,
                "email": user.email,
            }

    except Exception:
        pass

    # רק אם אתה עדיין צריך custom HS256 ב-INTERNAL
    if APP_MODE == "internal":
        secret = (
            os.getenv("SUPABASE_JWT_SECRET")
            or os.getenv("JWT_SECRET")
        )

        if not secret:
            raise HTTPException(
                status_code=500,
                detail="JWT secret is not configured",
            )

        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail="Token expired",
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired token",
            )

    raise HTTPException(
        status_code=401,
        detail="Invalid or expired token",
    )
