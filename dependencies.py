from fastapi import Header, HTTPException
from supabase import Client, create_client
import jwt
import os


def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
    )

    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required"
        )

    return create_client(url, key)


def get_current_user(
    authorization: str = Header(...),
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing authorization header",
        )

    token = authorization.removeprefix("Bearer ").strip()

    # Supabase access token מה-UI
    try:
        db = get_supabase()
        user_response = db.auth.get_user(token)

        if user_response and user_response.user:
            user = user_response.user

            return {
                "uid": user.id,
                "sub": user.email,
                "email": user.email,
            }
    except Exception:
        pass

    # Custom HS256 token
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
