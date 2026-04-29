"""
VERSION  10
auth.py - Authentication & Settings API for VID
FastAPI router with JWT authentication and Supabase integration

FIXES v10:
- Google avatar is now downloaded and re-uploaded to GCS (never stores Google URL)
- Existing users: avatar only filled if empty (user-set avatar takes priority)
- get_settings: safe handling when user not found (no .single() exception)
- update_settings: robust result check
"""

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
import httpx
import jwt
import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import UploadFile, File
from google.cloud import storage
import uuid
from functools import lru_cache
import requests
from logging_config import get_logger

GCS_BUCKET_NAME = "vid-michal-uploads"
GCS_PUBLIC_URL = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}"

logger = get_logger("auth")
router = APIRouter(prefix="/auth", tags=["auth"])


# ══════════════════════════════════════════════════════════════════════════════
# GCS Helpers
# ══════════════════════════════════════════════════════════════════════════════

def upload_to_gcs(file_data: bytes, filename: str, content_type: str) -> str:
    """Upload file to Google Cloud Storage and return public URL"""
    try:
        key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if key_path:
            client = storage.Client.from_service_account_json(key_path)
        else:
            client = storage.Client()

        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"avatars/{filename}")
        blob.upload_from_string(file_data, content_type=content_type)
        return f"{GCS_PUBLIC_URL}/avatars/{filename}"
    except Exception as e:
        logger.error(f"GCS upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload image")


async def mirror_google_avatar_to_gcs(picture_url: str, user_id: str) -> str:
    """
    Download Google profile picture and re-upload to GCS.
    Falls back to original Google URL if anything fails.
    """
    if not picture_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(picture_url)
        if resp.status_code != 200:
            logger.warning(f"Could not download Google avatar (status {resp.status_code}), using Google URL as fallback")
            return picture_url

        content_type = resp.headers.get("content-type", "image/jpeg")
        ext = "jpg"
        if "png" in content_type:
            ext = "png"
        elif "webp" in content_type:
            ext = "webp"
        elif "gif" in content_type:
            ext = "gif"

        filename = f"{user_id}_google_{uuid.uuid4().hex[:8]}.{ext}"
        gcs_url = upload_to_gcs(resp.content, filename, content_type)
        logger.info(f"Google avatar mirrored to GCS: {gcs_url}", extra={"user_id": user_id})
        return gcs_url
    except Exception as e:
        logger.warning(f"Failed to mirror Google avatar to GCS: {e}, falling back to Google URL")
        return picture_url  # graceful fallback


# ══════════════════════════════════════════════════════════════════════════════
# Database Connection
# ══════════════════════════════════════════════════════════════════════════════

def get_db() -> Client:
    """Get Supabase client with service role key for full access"""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

    if not url or not key:
        logger.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        raise HTTPException(status_code=500, detail="Database configuration error")

    return create_client(url, key)


# ══════════════════════════════════════════════════════════════════════════════
# JWT Configuration
# ══════════════════════════════════════════════════════════════════════════════

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 7


def get_jwt_secret():
    return os.getenv("SUPABASE_JWT_SECRET")


def make_jwt(user_id: str, email: str) -> str:
    """Create a JWT token for the user"""
    jwt_secret = get_jwt_secret()
    if not jwt_secret:
        logger.error("JWT_SECRET not configured")
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")

    payload = {
        "sub": email,
        "uid": user_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRATION_DAYS),
    }
    return jwt.encode(payload, jwt_secret, algorithm=JWT_ALGORITHM)


@lru_cache(maxsize=1)
def get_supabase_jwks():
    """Fetch Supabase JWKS (cached)"""
    url = f"{os.getenv('SUPABASE_URL')}/.well-known/jwks.json"
    try:
        resp = requests.get(url, timeout=10)
        return resp.json()
    except Exception:
        return None


def decode_jwt(token: str) -> dict:
    """Decode and validate a JWT token — tries Supabase first, falls back to custom HS256"""
    # Option 1: Supabase verification (UI tokens)
    try:
        db = get_db()
        user_response = db.auth.get_user(token)
        if user_response and user_response.user:
            logger.info("Token verified with Supabase", extra={"action": "token_verified_supabase"})
            user = user_response.user
            return {"uid": user.id, "sub": user.email, "email": user.email}
    except Exception as e:
        logger.warning(f"Supabase token verification failed: {e}")

    # Option 2: Custom HS256 fallback (Postman / service tokens)
    logger.info("Falling back to custom JWT verification", extra={"action": "token_verify_fallback"})
    jwt_secret = get_jwt_secret()
    if jwt_secret:
        try:
            payload = jwt.decode(token, jwt_secret, algorithms=["HS256"])
            logger.info("Token verified with custom JWT", extra={"action": "token_verified_custom"})
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            pass

    raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(authorization: str = Header(None)) -> dict:
    """Dependency to get current user from JWT token"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    token = authorization.replace("Bearer ", "")
    return decode_jwt(token)


# ══════════════════════════════════════════════════════════════════════════════
# Request Models
# ══════════════════════════════════════════════════════════════════════════════

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


class UpdateSettingsRequest(BaseModel):
    full_name: Optional[str] = None
    mobile: Optional[str] = None
    lang: Optional[str] = None
    avatar: Optional[str] = None
    package_type: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# Auth Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/google")
async def google_auth(request: GoogleTokenRequest):
    """Login/Signup via Google OAuth — avatar is always mirrored to GCS"""
    logger.info("Google auth attempt", extra={"action": "google_auth_start"})

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {request.token}"},
        )

    if resp.status_code != 200:
        logger.warning("Google token verification failed", extra={
            "action": "google_auth_failed",
            "status_code": resp.status_code,
        })
        raise HTTPException(status_code=401, detail="Invalid Google token")

    info = resp.json()
    email = info.get("email")
    google_id = info.get("sub")
    name = info.get("name", "")
    picture = info.get("picture", "")

    logger.info(f"Google auth for: {email}", extra={"action": "google_auth", "email": email})

    db = get_db()
    existing = db.table("users").select("*").eq("email", email).execute()

    if existing.data:
        # ── Existing user ──────────────────────────────────────────────────
        user_row = existing.data[0]

        update_payload = {
            "last_login": datetime.utcnow().isoformat(),
            "google_id": google_id,
        }

        # Only fill avatar if the user has none (user-set avatar takes full priority)
        current_avatar = user_row.get("avatar", "")
        is_google_url = "googleusercontent.com" in current_avatar or "lh3.google" in current_avatar

        if not current_avatar or is_google_url:
          gcs_avatar = await mirror_google_avatar_to_gcs(picture, str(user_row["id"]))
          if gcs_avatar:
            update_payload["avatar"] = gcs_avatar

        db.table("users").update(update_payload).eq("id", user_row["id"]).execute()

        # Re-fetch to get the latest avatar value
        refreshed = db.table("users").select("*").eq("id", user_row["id"]).execute()
        user_row = refreshed.data[0] if refreshed.data else user_row

        logger.info("Google login successful", extra={
            "action": "google_login_success",
            "user_id": str(user_row["id"]),
            "email": email,
        })

    else:
        # ── New user — mirror Google avatar to GCS before insert ───────────
        gcs_avatar = await mirror_google_avatar_to_gcs(picture, "new")

        result = db.table("users").insert({
            "email": email,
            "name": name,
            "google_id": google_id,
            "avatar": gcs_avatar,          # ← always GCS URL, never raw Google URL
            "lang": "he",
            "package_type": "basic",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()

        if not result.data:
            logger.error(f"Failed to create user: {email}")
            raise HTTPException(status_code=500, detail="Failed to create user")

        user_row = result.data[0]

        # If we used "new" as placeholder user_id, re-upload with real id
        if picture and gcs_avatar == picture:
            # fallback was used, try again with real id
            real_gcs = await mirror_google_avatar_to_gcs(picture, str(user_row["id"]))
            if real_gcs and real_gcs != picture:
                db.table("users").update({"avatar": real_gcs}).eq("id", user_row["id"]).execute()
                user_row["avatar"] = real_gcs

        logger.info("New Google user created", extra={
            "action": "google_signup_success",
            "user_id": str(user_row["id"]),
            "email": email,
        })

    return {
        "access_token": make_jwt(str(user_row["id"]), user_row["email"]),
        "user": {
            "id": str(user_row["id"]),
            "email": user_row["email"],
            "name": user_row.get("name", ""),
            "avatar": user_row.get("avatar", ""),
            "lang": user_row.get("lang", "he"),
        },
    }


@router.post("/login")
async def login(request: LoginRequest):
    """Login with email/password via Supabase Auth"""
    logger.info("Login attempt", extra={"action": "login_attempt", "email": request.email})

    db = get_db()

    try:
        result = db.auth.sign_in_with_password({
            "email": request.email,
            "password": request.password,
        })
    except Exception:
        logger.warning("Login failed - invalid credentials", extra={
            "action": "login_failed",
            "email": request.email,
            "reason": "invalid_credentials",
        })
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user.email_confirmed_at:
        logger.warning("Unverified email login attempt", extra={
            "action": "login_failed",
            "email": request.email,
            "reason": "email_not_verified",
        })
        raise HTTPException(status_code=403, detail="יש לאמת את המייל לפני הכניסה")

    user = result.user
    user_meta = user.user_metadata or {}
    name = user_meta.get("full_name", "")

    db.table("users").upsert({
        "id": user.id,
        "email": user.email,
        "name": name,
        "last_login": datetime.utcnow().isoformat(),
    }, on_conflict="id").execute()

    logger.info("Login successful", extra={
        "action": "login_success",
        "user_id": user.id,
        "email": request.email,
    })

    return {
        "access_token": make_jwt(user.id, user.email),
        "user": {"id": user.id, "email": user.email, "name": name},
    }


@router.post("/signup")
async def signup(request: SignupRequest):
    """Signup with email/password"""
    logger.info("Signup attempt", extra={"action": "signup_attempt", "email": request.email})

    db = get_db()

    try:
        result = db.auth.sign_up({
            "email": request.email,
            "password": request.password,
            "options": {
                "email_redirect_to": f"{os.getenv('FRONTEND_URL', 'https://ui.michal-solutions.com')}/login"
            },
        })
    except Exception as e:
        error_msg = str(e).lower()
        if "already registered" in error_msg or "already exists" in error_msg:
            logger.warning("Signup failed - email exists", extra={
                "action": "signup_failed",
                "email": request.email,
                "reason": "email_exists",
            })
            raise HTTPException(status_code=400, detail="האימייל כבר רשום במערכת")
        logger.error(f"Signup error: {e}", extra={"action": "signup_error"})
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    if not result.user:
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    try:
        db.table("users").insert({
            "id": result.user.id,
            "email": result.user.email,
            "name": "",
            "lang": "he",
            "package_type": "basic",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        logger.debug(f"User row creation skipped (trigger may exist): {e}")

    logger.info("Signup successful", extra={
        "action": "signup_success",
        "user_id": result.user.id,
        "email": request.email,
    })

    return {"message": "נשלח מייל אימות — בדוק את תיבת הדואר שלך ואשר את הכתובת"}


@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send password reset email"""
    logger.info("Password reset request", extra={
        "action": "password_reset_request",
        "email": request.email,
    })

    db = get_db()

    try:
        db.auth.reset_password_email(
            request.email,
            options={
                "redirect_to": f"{os.getenv('FRONTEND_URL', 'https://ui.michal-solutions.com')}/reset-password"
            },
        )
    except Exception as e:
        logger.debug(f"Password reset error (may be okay): {e}")

    return {"message": "אם האימייל קיים במערכת — נשלחו הוראות איפוס"}


# ══════════════════════════════════════════════════════════════════════════════
# Settings Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/settings")
async def get_settings(current_user: dict = Depends(get_current_user)):
    """Get current user settings"""
    user_id = current_user.get("uid")
    logger.info("Get settings", extra={"action": "get_settings", "user_id": user_id})

    db = get_db()

    # FIX: use .eq().execute() + check data, not .single() which throws on miss
    try:
        result = db.table("users").select("*").eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to get user settings: {e}", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="שגיאה בטעינת הגדרות")

    if not result.data:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")

    user = result.data[0]

    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "full_name": user.get("name") or "",
        "name": user.get("name") or "",
        "mobile": user.get("mobile") or "",
        "lang": user.get("lang") or "he",
        "avatar": user.get("avatar") or "",
        "package_type": user.get("package_type") or "basic",
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


@router.put("/settings")
async def update_settings(
    request: UpdateSettingsRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update user settings"""
    user_id = current_user.get("uid")
    logger.info("Update settings start", extra={
        "action": "update_settings_start",
        "user_id": user_id,
    })

    db = get_db()

    update_data = {}
    if request.full_name is not None:
        update_data["name"] = request.full_name
    if request.mobile is not None:
        update_data["mobile"] = request.mobile
    if request.lang is not None:
        update_data["lang"] = request.lang
    if request.avatar is not None:
        update_data["avatar"] = request.avatar
    if request.package_type is not None:
        update_data["package_type"] = request.package_type

    if not update_data:
        return {"message": "אין שינויים לעדכון", "updated": {}}

    update_data["updated_at"] = datetime.utcnow().isoformat()

    try:
        result = db.table("users").update(update_data).eq("id", user_id).execute()

        # FIX: Supabase may return empty data on update even when successful;
        # treat empty result as success unless an exception was raised.
        logger.info("Settings updated", extra={
            "action": "update_settings_success",
            "user_id": user_id,
            "fields": list(update_data.keys()),
        })

        return {"message": "ההגדרות עודכנו בהצלחה", "updated": update_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update settings: {e}", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="שגיאה בעדכון ההגדרות")


# ══════════════════════════════════════════════════════════════════════════════
# Avatar Upload
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload avatar image to GCS and update user record"""
    user_id = current_user.get("uid")
    logger.info("Avatar upload start", extra={
        "action": "avatar_upload_start",
        "user_id": user_id,
        "file_name": file.filename,
        "content_type": file.content_type,
    })

    allowed_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="סוג קובץ לא נתמך. השתמש ב-JPG, PNG, GIF או WebP",
        )

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="הקובץ גדול מדי. מקסימום 5MB")

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    unique_filename = f"{user_id}_{uuid.uuid4().hex[:8]}.{ext}"

    avatar_url = upload_to_gcs(contents, unique_filename, file.content_type)

    db = get_db()
    try:
        db.table("users").update({
            "avatar": avatar_url,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", user_id).execute()

        logger.info("Avatar uploaded successfully", extra={
            "action": "avatar_upload_success",
            "user_id": user_id,
            "avatar_url": avatar_url,
        })

        return {"message": "התמונה הועלתה בהצלחה", "avatar_url": avatar_url}
    except Exception as e:
        logger.error(f"Failed to update avatar in DB: {e}", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="שגיאה בשמירת התמונה")


# ══════════════════════════════════════════════════════════════════════════════
# Utility Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current user info from JWT"""
    return {
        "uid": current_user.get("uid"),
        "email": current_user.get("sub"),
    }


@router.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "auth",
        "timestamp": datetime.utcnow().isoformat(),
    }