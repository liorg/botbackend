from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from supabase import create_client
import httpx, jwt, os
from datetime import datetime, timedelta

router = APIRouter(prefix="/auth", tags=["auth"])

def get_db():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def make_jwt(user_id: str, email: str) -> str:
    return jwt.encode({
        "sub": email,
        "uid": user_id,
        "exp": datetime.utcnow() + timedelta(days=7)
    }, os.getenv("JWT_SECRET"), algorithm="HS256")

class GoogleTokenRequest(BaseModel):
    token: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class SignupRequest(BaseModel):
    email: EmailStr
    password: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

@router.post("/google")
async def google_auth(request: GoogleTokenRequest):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {request.token}"}
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    info = resp.json()
    db   = get_db()

    existing = db.table("users").select("*").eq("email", info["email"]).execute()
    if existing.data:
        user_row = existing.data[0]
        db.table("users").update({"last_login": datetime.utcnow().isoformat()}).eq("id", user_row["id"]).execute()
    else:
        result = db.table("users").insert({
            "email": info["email"], "name": info.get("name", ""),
            "google_id": info["sub"], "avatar": info.get("picture", ""),
        }).execute()
        user_row = result.data[0]

    return {
        "access_token": make_jwt(user_row["id"], user_row["email"]),
        "user": {"id": user_row["id"], "email": user_row["email"],
                 "name": user_row.get("name", ""), "avatar": user_row.get("avatar", "")}
    }

@router.post("/login")
async def login(request: LoginRequest):
    db = get_db()
    try:
        result = db.auth.sign_in_with_password({"email": request.email, "password": request.password})
    except Exception:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user.email_confirmed_at:
        raise HTTPException(status_code=403, detail="יש לאמת את המייל לפני הכניסה")

    user = result.user
    name = user.user_metadata.get("full_name", "") if user.user_metadata else ""
    return {
        "access_token": make_jwt(user.id, user.email),
        "user": {"id": user.id, "email": user.email, "name": name}
    }

@router.post("/signup")
async def signup(request: SignupRequest):
    db = get_db()
    try:
        result = db.auth.sign_up({"email": request.email, "password": request.password})
    except Exception as e:
        if "already registered" in str(e):
            raise HTTPException(status_code=400, detail="האימייל כבר רשום במערכת")
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    if not result.user:
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    return {"message": "נשלח מייל אימות — בדוק את תיבת הדואר שלך ואשר את הכתובת"}

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    db = get_db()
    try:
        db.auth.reset_password_email(
            request.email,
            options={"redirect_to": f"{os.getenv('FRONTEND_URL')}/reset-password"}
        )
    except Exception:
        pass
    return {"message": "אם האימייל קיים במערכת — נשלחו הוראות איפוס"}
