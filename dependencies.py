from fastapi import Header, HTTPException, Depends
from supabase import create_client, Client
import jwt, os

def get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def get_current_user(authorization: str = Header(...)):
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, os.getenv("JWT_SECRET"), algorithms=["HS256"])
        return payload  # {"sub": email, "uid": user_id}
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
