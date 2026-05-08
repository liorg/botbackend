from fastapi import Header, HTTPException, Depends
from supabase import create_client, Client
import jwt, os

def get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def get_current_user(authorization: str = Header(...)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization header")

    token = authorization.replace("Bearer ", "")

    # Option 1: Supabase token (מה-UI)
    try:
        db = get_supabase()
        user_response = db.auth.get_user(token)
        if user_response and user_response.user:
            user = user_response.user
            return {"uid": user.id, "sub": user.email, "email": user.email}
    except Exception:
        pass

    # Option 2: Custom HS256 (מ-Postman)
    try:
        payload = jwt.decode(
            token,
            os.getenv("SUPABASE_JWT_SECRET") or os.getenv("JWT_SECRET"),
            algorithms=["HS256"]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        pass

    raise HTTPException(status_code=401, detail="Invalid or expired token")