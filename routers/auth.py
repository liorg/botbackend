from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client
import httpx, jwt, os
from datetime import datetime, timedelta
from typing import Optional

router = APIRouter(prefix="/auth", tags=["auth"])

# ── Database ─────────────────────────────────────────────────────────────────
def get_db():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# ── JWT ──────────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 7

def make_jwt(user_id: str, email: str) -> str:
    return jwt.encode({
        "sub": email,
        "uid": user_id,
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRATION_DAYS)
    }, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    token = authorization.replace("Bearer ", "")
    return decode_jwt(token)

# ── Request Models ───────────────────────────────────────────────────────────
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

class UpdatePasswordRequest(BaseModel):
    new_password: str

class UpdateSettingsRequest(BaseModel):
    full_name: Optional[str] = None
    mobile: Optional[str] = None
    lang: Optional[str] = None
    avatar: Optional[str] = None

# ── Auth Endpoints ───────────────────────────────────────────────────────────
@router.post("/google")
async def google_auth(request: GoogleTokenRequest):
    """Login/Signup via Google OAuth"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {request.token}"}
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    info = resp.json()
    db = get_db()

    existing = db.table("users").select("*").eq("email", info["email"]).execute()
    if existing.data:
        user_row = existing.data[0]
        db.table("users").update({
            "last_login": datetime.utcnow().isoformat()
        }).eq("id", user_row["id"]).execute()
    else:
        result = db.table("users").insert({
            "email": info["email"],
            "name": info.get("name", ""),
            "google_id": info["sub"],
            "avatar": info.get("picture", ""),
        }).execute()
        user_row = result.data[0]

    return {
        "access_token": make_jwt(user_row["id"], user_row["email"]),
        "user": {
            "id": user_row["id"],
            "email": user_row["email"],
            "name": user_row.get("name", ""),
            "avatar": user_row.get("avatar", "")
        }
    }

@router.post("/login")
async def login(request: LoginRequest):
    """Login with email/password"""
    db = get_db()
    try:
        result = db.auth.sign_in_with_password({
            "email": request.email,
            "password": request.password
        })
    except Exception:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user.email_confirmed_at:
        raise HTTPException(status_code=403, detail="יש לאמת את המייל לפני הכניסה")

    user = result.user
    name = user.user_metadata.get("full_name", "") if user.user_metadata else ""
    
    # Update last_login in public.users
    db.table("users").upsert({
        "id": user.id,
        "email": user.email,
        "last_login": datetime.utcnow().isoformat(),
    }).execute()

    return {
        "access_token": make_jwt(user.id, user.email),
        "user": {
            "id": user.id,
            "email": user.email,
            "name": name
        }
    }

@router.post("/signup")
async def signup(request: SignupRequest):
    """Signup with email/password"""
    db = get_db()
    try:
        result = db.auth.sign_up({
            "email": request.email,
            "password": request.password
        })
    except Exception as e:
        if "already registered" in str(e):
            raise HTTPException(status_code=400, detail="האימייל כבר רשום במערכת")
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    if not result.user:
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    # Create entry in public.users (will be updated when email is confirmed)
    try:
        db.table("users").insert({
            "id": result.user.id,
            "email": result.user.email,
            "name": "",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
    except Exception:
        pass  # May fail if trigger already created the row

    return {"message": "נשלח מייל אימות — בדוק את תיבת הדואר שלך ואשר את הכתובת"}

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send password reset email"""
    db = get_db()
    try:
        db.auth.reset_password_email(
            request.email,
            options={"redirect_to": f"{os.getenv('FRONTEND_URL')}/reset-password"}
        )
    except Exception:
        pass  # Don't reveal if email exists
    return {"message": "אם האימייל קיים במערכת — נשלחו הוראות איפוס"}

@router.post("/update-password")
async def update_password(
    request: UpdatePasswordRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update password (requires authentication)"""
    db = get_db()
    try:
        # Note: This requires the user to be authenticated via Supabase session
        # The JWT we use is custom, so we need to use Supabase's method
        db.auth.update_user({"password": request.new_password})
        return {"message": "הסיסמה עודכנה בהצלחה"}
    except Exception as e:
        raise HTTPException(status_code=400, detail="שגיאה בעדכון הסיסמה")

# ── Settings Endpoints ───────────────────────────────────────────────────────
@router.get("/settings")
async def get_settings(current_user: dict = Depends(get_current_user)):
    """Get current user settings"""
    db = get_db()
    user_id = current_user.get("uid")
    
    result = db.table("users").select("*").eq("id", user_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    
    user = result.data
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "full_name": user.get("name"),
        "mobile": user.get("mobile"),
        "lang": user.get("lang", "he"),
        "avatar": user.get("avatar"),
        "package_type": user.get("package_type", "basic"),
        "created_at": user.get("created_at"),
    }

@router.put("/settings")
async def update_settings(
    request: UpdateSettingsRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update user settings"""
    db = get_db()
    user_id = current_user.get("uid")
    
    # Build update dict (only non-None values)
    update_data = {}
    if request.full_name is not None:
        update_data["name"] = request.full_name
    if request.mobile is not None:
        update_data["mobile"] = request.mobile
    if request.lang is not None:
        update_data["lang"] = request.lang
    if request.avatar is not None:
        update_data["avatar"] = request.avatar
    
    if not update_data:
        return {"message": "אין שינויים לעדכון"}
    
    update_data["updated_at"] = datetime.utcnow().isoformat()
    
    try:
        db.table("users").update(update_data).eq("id", user_id).execute()
        return {"message": "ההגדרות עודכנו בהצלחה", "updated": update_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail="שגיאה בעדכון ההגדרות")

# ── Health Check ─────────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}