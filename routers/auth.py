"""
auth.py — FastAPI router
User authentication, profile settings, avatar storage and 2FA (WhatsApp OTP).

Token policy
------------
This router does NOT mint its own JWT. Every token it returns is a real
Supabase access token, which is exactly what dependencies._verify_user_token
expects. The previous make_jwt() produced an HS256 token with sub=<email> and
no "aud" claim, so it was rejected by every other router (401) — and when it
was accepted, uid resolved to the email address.

Authentication and the Supabase client both come from dependencies.py.
Nothing in this file creates a Supabase client per request.

2FA
---
two_factor_verified is server-managed: the DB trigger users_guard_2fa resets
it on any mobile change and blocks anyone but service_role from setting it.
OTP codes live in user_otp (service_role only) as an HMAC. POST /2fa/request
does not send anything: it inserts a pending row into request_otp, and a
separate job sends it on WhatsApp.
"""

import hashlib
import hmac
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr, Field, field_validator
from supabase import Client

from dependencies import (
    get_current_user,
    get_service_supabase,
    get_supabase,
)
from google.cloud import storage
from logging_config import get_logger

logger = get_logger("auth")

router = APIRouter(prefix="/auth", tags=["auth"])

GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "vid-michal-uploads")
GCS_PUBLIC_URL = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}"
GCS_AVATAR_PREFIX = "avatars"

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ui.michal-solutions.com")

MAX_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
AVATAR_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

SUPPORTED_LANGS = {"he", "en", "ru", "ar"}

# ── 2FA / OTP ─────────────────────────────────────────────────────────────────
OTP_TTL_SEC = int(os.getenv("OTP_TTL_SEC", "300"))
OTP_RESEND_SEC = int(os.getenv("OTP_RESEND_SEC", "60"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
OTP_SECRET = os.getenv("OTP_SECRET", "")
# Sending is done by a separate job that reads request_otp (status='pending').

OTP_TEXT = {
    "he": "קוד האימות שלך ב-Grossman.Bot: {code}\nהקוד תקף ל-{minutes} דקות. אין להעביר אותו לאף אחד.",
    "en": "Your Grossman.Bot verification code: {code}\nValid for {minutes} minutes. Do not share it with anyone.",
    "ru": "Ваш код подтверждения Grossman.Bot: {code}\nДействует {minutes} мин. Никому его не сообщайте.",
    "ar": "رمز التحقق الخاص بك في Grossman.Bot: {code}\nصالح لمدة {minutes} دقائق. لا تشاركه مع أي شخص.",
}

DOMAIN_RE = re.compile(
    r"^(\*\.)?(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)

# Columns returned to a browser. Never replace with select("*"): the users
# table also carries provider ids and internal billing flags.
USER_COLUMNS = (
    "id, email, name, mobile, lang, avatar, package_type, "
    "two_factor_verified, two_factor_verified_at, "
    "trusted_domain, notify_email, notify_errors_only, "
    "created_at, updated_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _public_db() -> Client:
    """Service-role client for endpoints that run before a token exists."""
    try:
        return get_service_supabase()
    except RuntimeError as e:
        # A config error raised here would bypass CORSMiddleware and reach the
        # browser as an opaque CORS failure.
        logger.error(f"Service client unavailable: {e}")
        raise HTTPException(status_code=503, detail="Auth backend not configured")


# ══════════════════════════════════════════════════════════════════════════════
# Google Cloud Storage
# ══════════════════════════════════════════════════════════════════════════════

def _gcs_client() -> storage.Client:
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if key_path:
        return storage.Client.from_service_account_json(key_path)
    return storage.Client()


def upload_to_gcs(file_data: bytes, filename: str, content_type: str) -> str:
    """Upload bytes to the avatars prefix and return the public URL."""
    try:
        blob = _gcs_client().bucket(GCS_BUCKET_NAME).blob(
            f"{GCS_AVATAR_PREFIX}/{filename}"
        )
        blob.upload_from_string(file_data, content_type=content_type)
        return f"{GCS_PUBLIC_URL}/{GCS_AVATAR_PREFIX}/{filename}"
    except Exception as e:
        logger.error(f"GCS upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload image")


def _is_gcs_avatar(url: str) -> bool:
    return bool(url) and url.startswith(GCS_PUBLIC_URL)


async def mirror_google_avatar_to_gcs(picture_url: str, user_id: str) -> str:
    """Copy a Google profile picture into GCS.

    Returns the GCS URL, or the original Google URL if anything fails, so a
    storage outage never blocks a login.
    """
    if not picture_url:
        return ""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(picture_url)

        if resp.status_code != 200:
            logger.warning(
                f"Could not download Google avatar (status {resp.status_code}) — "
                "keeping the Google URL"
            )
            return picture_url

        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        ext = AVATAR_EXTENSIONS.get(content_type, "jpg")
        filename = f"{user_id}_google_{uuid.uuid4().hex[:8]}.{ext}"

        gcs_url = upload_to_gcs(resp.content, filename, content_type)
        logger.info("Google avatar mirrored to GCS", extra={"user_id": user_id})
        return gcs_url
    except Exception as e:
        logger.warning(f"Failed to mirror Google avatar: {e} — keeping the Google URL")
        return picture_url


async def _ensure_gcs_avatar(db: Client, user_id: str, current: str, source: str) -> str:
    """Single owner of the avatar rule, used by both /google and /settings.

    A user-uploaded GCS avatar always wins. Only an empty avatar or a raw
    Google URL is replaced, and the DB row is updated in place.
    """
    if _is_gcs_avatar(current):
        return current
    if not source:
        return current

    mirrored = await mirror_google_avatar_to_gcs(source, user_id)
    if not _is_gcs_avatar(mirrored):
        return current or mirrored

    db.table("users").update(
        {"avatar": mirrored, "updated_at": _now()}
    ).eq("id", user_id).execute()
    return mirrored


# ══════════════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════════════

class GoogleTokenRequest(BaseModel):
    token: str = Field(
        ...,
        description=(
            "Google OIDC ID token from the client. This is the id_token, not "
            "an OAuth access token — Supabase verifies its signature."
        ),
    )
    nonce: Optional[str] = Field(
        None,
        description="Raw nonce, required only when the ID token was requested with one.",
    )


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., description="Registered email address.")
    password: str = Field(..., min_length=6, description="Account password.")


class SignupRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address to register.")
    password: str = Field(..., min_length=6, description="Password, at least 6 characters.")
    name: Optional[str] = Field(None, max_length=120, description="Display name.")
    lang: str = Field("he", max_length=5, description="UI language code: he, en, ru or ar.")


class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address to send reset instructions to.")


class UpdateSettingsRequest(BaseModel):
    # Fields deliberately absent:
    #   package_type        — billing field; accepting it let any caller upgrade
    #                         their own plan with a PUT.
    #   avatar              — writable only through POST /auth/avatar.
    #   two_factor_verified — set only by POST /auth/2fa/verify.
    full_name: Optional[str] = Field(None, max_length=120, description="Display name.")
    mobile: Optional[str] = Field(
        None, max_length=32,
        description="Mobile number. Changing it resets two_factor_verified.",
    )
    lang: Optional[str] = Field(None, max_length=5, description="UI language code: he, en, ru or ar.")
    trusted_domain: Optional[str] = Field(
        None, max_length=253,
        description="Trusted domain, e.g. example.com or *.example.com. Empty string clears it.",
    )
    notify_email: Optional[bool] = Field(None, description="Send notifications by email.")
    notify_errors_only: Optional[bool] = Field(
        None, description="When notify_email is on: true = errors only, false = every notification.",
    )

    @field_validator("lang")
    @classmethod
    def _lang(cls, v):
        if v is not None and v not in SUPPORTED_LANGS:
            raise ValueError("lang must be one of: he, en, ru, ar")
        return v

    @field_validator("trusted_domain")
    @classmethod
    def _domain(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        v = re.sub(r"^https?://", "", v).rstrip("/")
        if v == "":
            return ""
        if not DOMAIN_RE.match(v):
            raise ValueError("invalid domain")
        return v


class UserPublic(BaseModel):
    id: str = Field(..., description="Supabase user id.")
    email: Optional[str] = Field(None, description="Email address.")
    name: str = Field("", description="Display name.")
    avatar: str = Field("", description="Avatar URL, always a GCS URL once mirrored.")
    lang: str = Field("he", description="UI language code.")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="Supabase access token. Send as: Authorization: Bearer <token>.")
    refresh_token: Optional[str] = Field(None, description="Supabase refresh token.")
    token_type: str = Field("bearer", description="Always bearer.")
    expires_in: Optional[int] = Field(None, description="Seconds until the access token expires.")
    user: UserPublic = Field(..., description="The authenticated user.")


class SettingsResponse(BaseModel):
    id: str = Field(..., description="Supabase user id.")
    email: Optional[str] = Field(None, description="Email address.")
    full_name: str = Field("", description="Display name. Mirrors name, kept for the existing UI.")
    name: str = Field("", description="Display name.")
    mobile: str = Field("", description="Mobile number.")
    lang: str = Field("he", description="UI language code.")
    avatar: str = Field("", description="Avatar URL.")
    package_type: str = Field("basic", description="Billing plan. Read-only on this API.")
    two_factor_verified: bool = Field(False, description="Mobile verified by WhatsApp OTP.")
    two_factor_verified_at: Optional[str] = Field(None, description="Verification time, ISO 8601.")
    trusted_domain: str = Field("", description="Trusted domain.")
    notify_email: bool = Field(False, description="Send notifications by email.")
    notify_errors_only: bool = Field(True, description="Email errors only (when notify_email is on).")
    created_at: Optional[str] = Field(None, description="Row creation timestamp, ISO 8601.")
    updated_at: Optional[str] = Field(None, description="Last update timestamp, ISO 8601.")


class UpdateSettingsResponse(BaseModel):
    message: str = Field(..., description="Human-readable result.")
    updated: dict = Field(default_factory=dict, description="Fields that were written.")


class AvatarUploadResponse(BaseModel):
    message: str = Field(..., description="Human-readable result.")
    avatar_url: str = Field(..., description="Public GCS URL of the stored avatar.")


class MessageResponse(BaseModel):
    message: str = Field(..., description="Human-readable result.")


class OtpRequestResponse(BaseModel):
    message: str = Field(..., description="Human-readable result.")
    sent_to: str = Field(..., description="Masked destination number.")
    expires_in: int = Field(..., description="Seconds until the code expires.")
    resend_in: int = Field(..., description="Seconds until a new code may be requested.")


class OtpVerifyRequest(BaseModel):
    code: str = Field(..., pattern=r"^\d{6}$", description="The 6-digit code received on WhatsApp.")


class OtpVerifyResponse(BaseModel):
    message: str = Field(..., description="Human-readable result.")
    two_factor_verified: bool = Field(..., description="Always true on success.")
    verified_at: str = Field(..., description="Verification time, ISO 8601.")


class MeResponse(BaseModel):
    uid: str = Field(..., description="Supabase user id taken from the verified token.")
    email: Optional[str] = Field(None, description="Email claim from the token.")
    role: Optional[str] = Field(None, description="Supabase role claim.")


class HealthResponse(BaseModel):
    status: str = Field(..., description="Always ok when the router is reachable.")
    service: str = Field(..., description="Service name.")
    timestamp: str = Field(..., description="Server time, ISO 8601 UTC.")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _session_response(session, user, row: dict) -> TokenResponse:
    return TokenResponse(
        access_token=session.access_token,
        refresh_token=getattr(session, "refresh_token", None),
        expires_in=getattr(session, "expires_in", None),
        user=UserPublic(
            id=str(user.id),
            email=user.email,
            name=row.get("name") or "",
            avatar=row.get("avatar") or "",
            lang=row.get("lang") or "he",
        ),
    )


def _upsert_user_row(db: Client, user, name: Optional[str] = None) -> dict:
    """Keep the users row in sync with Supabase Auth on every sign-in.

    name is written only when it carries a value: the previous version passed
    an empty string on every login and wiped names already stored.
    """
    payload = {
        "id": str(user.id),
        "email": user.email,
        "last_login": _now(),
    }
    if name:
        payload["name"] = name

    db.table("users").upsert(payload, on_conflict="id").execute()

    result = db.table("users").select(USER_COLUMNS).eq("id", str(user.id)).execute()
    return result.data[0] if result.data else {}


def _normalize_mobile(raw: Optional[str]) -> str:
    """Digits only, international form. 05x… → 9725x…, 00… → …"""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "972" + digits[1:]
    return digits


def _mask_mobile(digits: str) -> str:
    return f"+{digits[:3]}…{digits[-3:]}" if len(digits) > 6 else "+" + digits


def _otp_hash(user_id: str, code: str) -> str:
    if not OTP_SECRET:
        logger.error("OTP_SECRET is not set")
        raise HTTPException(status_code=503, detail="אימות דו-שלבי לא מוגדר בשרת")
    return hmac.new(
        OTP_SECRET.encode(), f"{user_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Authentication endpoints
#
# The browser signs in through supabase-js and never calls these. They exist
# for service clients and for Postman, and they return the same Supabase token
# the browser holds, so both paths authenticate identically everywhere else.
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/google",
    response_model=TokenResponse,
    summary="Sign in with a Google ID token",
    description=(
        "Exchanges a Google OIDC ID token for a Supabase session, creates the "
        "users row on first sign-in and mirrors the Google profile picture to "
        "GCS. A user-uploaded avatar is never overwritten."
    ),
)
async def google_auth(request: GoogleTokenRequest):
    db = _public_db()

    credentials = {"provider": "google", "token": request.token}
    if request.nonce:
        credentials["nonce"] = request.nonce

    try:
        result = db.auth.sign_in_with_id_token(credentials)
    except Exception as e:
        logger.warning(f"Google sign-in failed: {e}", extra={"action": "google_auth_failed"})
        raise HTTPException(status_code=401, detail="Invalid Google token")

    if not result.user or not result.session:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    user = result.user
    meta = user.user_metadata or {}

    row = _upsert_user_row(db, user, meta.get("full_name") or meta.get("name"))
    row["avatar"] = await _ensure_gcs_avatar(
        db,
        str(user.id),
        row.get("avatar") or "",
        meta.get("avatar_url") or meta.get("picture") or "",
    )

    logger.info("Google login successful", extra={
        "action": "google_login_success",
        "user_id": str(user.id),
    })
    return _session_response(result.session, user, row)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in with email and password",
    description=(
        "Signs in through Supabase Auth and returns the Supabase access token. "
        "Unverified email addresses are rejected with 403."
    ),
)
async def login(request: LoginRequest):
    db = _public_db()

    try:
        result = db.auth.sign_in_with_password({
            "email": request.email,
            "password": request.password,
        })
    except Exception:
        logger.warning("Login failed", extra={
            "action": "login_failed",
            "reason": "invalid_credentials",
        })
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user or not result.session:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    if not result.user.email_confirmed_at:
        logger.warning("Unverified email login attempt", extra={
            "action": "login_failed",
            "reason": "email_not_verified",
        })
        raise HTTPException(status_code=403, detail="יש לאמת את המייל לפני הכניסה")

    meta = result.user.user_metadata or {}
    row = _upsert_user_row(db, result.user, meta.get("full_name"))

    logger.info("Login successful", extra={
        "action": "login_success",
        "user_id": str(result.user.id),
    })
    return _session_response(result.session, result.user, row)


@router.post(
    "/signup",
    response_model=MessageResponse,
    summary="Register a new account",
    description=(
        "Creates a Supabase Auth user and sends the verification email. The "
        "users row is created by the DB trigger when one exists, otherwise here."
    ),
)
async def signup(request: SignupRequest):
    db = _public_db()

    try:
        result = db.auth.sign_up({
            "email": request.email,
            "password": request.password,
            "options": {
                "email_redirect_to": f"{FRONTEND_URL}/login",
                "data": {"full_name": request.name or "", "lang": request.lang},
            },
        })
    except Exception as e:
        message = str(e).lower()
        if "already registered" in message or "already exists" in message:
            logger.warning("Signup failed — email exists", extra={"action": "signup_failed"})
            raise HTTPException(status_code=400, detail="האימייל כבר רשום במערכת")
        logger.error(f"Signup error: {e}", extra={"action": "signup_error"})
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    if not result.user:
        raise HTTPException(status_code=400, detail="הרשמה נכשלה")

    try:
        db.table("users").insert({
            "id": str(result.user.id),
            "email": result.user.email,
            "name": request.name or "",
            "lang": request.lang,
            "package_type": "basic",
            "created_at": _now(),
        }).execute()
    except Exception as e:
        logger.debug(f"User row insert skipped, a trigger probably created it: {e}")

    logger.info("Signup successful", extra={
        "action": "signup_success",
        "user_id": str(result.user.id),
    })
    return MessageResponse(message="נשלח מייל אימות — בדוק את תיבת הדואר שלך ואשר את הכתובת")


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    summary="Request a password reset email",
    description=(
        "Always returns the same message whether or not the address exists, so "
        "the endpoint cannot be used to enumerate registered users."
    ),
)
async def forgot_password(request: ForgotPasswordRequest):
    db = _public_db()

    try:
        db.auth.reset_password_email(
            request.email,
            options={"redirect_to": f"{FRONTEND_URL}/reset-password"},
        )
    except Exception as e:
        logger.debug(f"Password reset error, reported as success by design: {e}")

    return MessageResponse(message="אם האימייל קיים במערכת — נשלחו הוראות איפוס")


# ══════════════════════════════════════════════════════════════════════════════
# Settings
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/settings",
    response_model=SettingsResponse,
    summary="Get my profile settings",
    description=(
        "Returns the profile of the authenticated user, including 2FA status, "
        "trusted domain and notification preferences. If no avatar is stored "
        "yet, the Google picture from the auth metadata is mirrored to GCS."
    ),
)
async def get_settings(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    user_id = user["uid"]

    try:
        result = db.table("users").select(USER_COLUMNS).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to load settings: {e}", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="שגיאה בטעינת הגדרות")

    if not result.data:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")

    row = result.data[0]
    avatar = row.get("avatar") or ""

    if not avatar:
        meta = user.get("user_metadata") or {}
        source = meta.get("avatar_url") or meta.get("picture") or ""
        avatar = await _ensure_gcs_avatar(db, user_id, avatar, source)

    return SettingsResponse(
        id=str(row.get("id")),
        email=row.get("email"),
        full_name=row.get("name") or "",
        name=row.get("name") or "",
        mobile=row.get("mobile") or "",
        lang=row.get("lang") or "he",
        avatar=avatar,
        package_type=row.get("package_type") or "basic",
        two_factor_verified=bool(row.get("two_factor_verified")),
        two_factor_verified_at=row.get("two_factor_verified_at"),
        trusted_domain=row.get("trusted_domain") or "",
        notify_email=bool(row.get("notify_email")),
        notify_errors_only=row.get("notify_errors_only") is not False,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@router.put(
    "/settings",
    response_model=UpdateSettingsResponse,
    summary="Update my profile settings",
    description=(
        "Updates name, mobile, language, trusted domain and notification "
        "preferences. Changing mobile resets two_factor_verified (DB trigger). "
        "The avatar is set through POST /auth/avatar; package_type cannot be changed."
    ),
)
async def update_settings(
    body: UpdateSettingsRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    user_id = user["uid"]

    # Built from the model, never from a raw dict: a raw body would let a
    # caller write id, package_type or any other column directly.
    patch = body.model_dump(exclude_none=True)
    if "full_name" in patch:
        patch["name"] = patch.pop("full_name")
    if patch.get("trusted_domain") == "":
        patch["trusted_domain"] = None

    if not patch:
        return UpdateSettingsResponse(message="אין שינויים לעדכון", updated={})

    patch["updated_at"] = _now()

    try:
        db.table("users").update(patch).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to update settings: {e}", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="שגיאה בעדכון ההגדרות")

    # Supabase returns an empty data array on update when the row is unchanged,
    # so an empty result is not an error here.
    logger.info("Settings updated", extra={
        "action": "update_settings_success",
        "user_id": user_id,
        "fields": list(patch.keys()),
    })
    return UpdateSettingsResponse(message="ההגדרות עודכנו בהצלחה", updated=patch)


# ══════════════════════════════════════════════════════════════════════════════
# 2FA — WhatsApp OTP
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/2fa/request",
    response_model=OtpRequestResponse,
    summary="Send a 6-digit verification code to my mobile on WhatsApp",
    description=(
        "Sends a code to the mobile number stored in settings. The code is valid "
        f"for {OTP_TTL_SEC}s, a new one can be requested after {OTP_RESEND_SEC}s, "
        f"and {OTP_MAX_ATTEMPTS} wrong attempts invalidate it."
    ),
)
async def request_2fa(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    user_id = user["uid"]

    res = db.table("users").select("mobile, lang").eq("id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")

    mobile = _normalize_mobile(res.data[0].get("mobile"))
    if len(mobile) < 9:
        raise HTTPException(status_code=400, detail="יש להזין מספר נייד תקין ולשמור")

    lang = res.data[0].get("lang") or "he"
    sdb = _public_db()
    now = datetime.now(timezone.utc)

    prev = sdb.table("user_otp").select("sent_at").eq("user_id", user_id).execute()
    if prev.data:
        elapsed = (now - _parse_ts(prev.data[0]["sent_at"])).total_seconds()
        if elapsed < OTP_RESEND_SEC:
            wait = int(OTP_RESEND_SEC - elapsed) + 1
            raise HTTPException(status_code=429, detail=f"ניתן לבקש קוד חדש בעוד {wait} שניות")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (now + timedelta(seconds=OTP_TTL_SEC)).isoformat()

    # user_otp — what verify checks (hash only)
    sdb.table("user_otp").upsert({
        "user_id": user_id,
        "mobile": mobile,
        "code_hash": _otp_hash(user_id, code),
        "attempts": 0,
        "sent_at": now.isoformat(),
        "expires_at": expires_at,
    }, on_conflict="user_id").execute()

    # request_otp — the queue the job reads. Older pending rows are cancelled,
    # so the job never sends a code that has already been replaced.
    sdb.table("request_otp").update({"status": "cancelled", "code": None, "message": None}) \
        .eq("user_id", user_id).eq("status", "pending").execute()

    text = OTP_TEXT.get(lang, OTP_TEXT["he"]).format(
        code=code, minutes=max(1, OTP_TTL_SEC // 60)
    )
    sdb.table("request_otp").insert({
        "user_id": user_id,
        "mobile": mobile,
        "lang": lang,
        "code": code,
        "message": text,
        "status": "pending",
        "expires_at": expires_at,
    }).execute()

    logger.info("2FA code queued", extra={"action": "otp_queued", "user_id": user_id})
    return OtpRequestResponse(
        message="הקוד יישלח ב-WhatsApp",
        sent_to=_mask_mobile(mobile),
        expires_in=OTP_TTL_SEC,
        resend_in=OTP_RESEND_SEC,
    )


@router.post(
    "/2fa/verify",
    response_model=OtpVerifyResponse,
    summary="Verify the 6-digit code and mark my mobile as verified",
    description=(
        "Compares the code against the active OTP. Fails if the code expired, "
        "the attempts ran out, or the mobile changed after the code was sent."
    ),
)
async def verify_2fa(
    body: OtpVerifyRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    user_id = user["uid"]
    sdb = _public_db()
    now = datetime.now(timezone.utc)

    res = sdb.table("user_otp").select("*").eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="אין קוד פעיל — בקש קוד חדש")
    otp = res.data[0]

    def _drop():
        sdb.table("user_otp").delete().eq("user_id", user_id).execute()

    if now > _parse_ts(otp["expires_at"]):
        _drop()
        raise HTTPException(status_code=400, detail="הקוד פג תוקף — בקש קוד חדש")

    if otp["attempts"] >= OTP_MAX_ATTEMPTS:
        _drop()
        raise HTTPException(status_code=429, detail="יותר מדי ניסיונות — בקש קוד חדש")

    if not hmac.compare_digest(otp["code_hash"], _otp_hash(user_id, body.code)):
        attempts = otp["attempts"] + 1
        left = OTP_MAX_ATTEMPTS - attempts
        if left <= 0:
            _drop()
            raise HTTPException(status_code=429, detail="יותר מדי ניסיונות — בקש קוד חדש")
        sdb.table("user_otp").update({"attempts": attempts}).eq("user_id", user_id).execute()
        raise HTTPException(status_code=400, detail=f"קוד שגוי. נותרו {left} ניסיונות")

    current = db.table("users").select("mobile").eq("id", user_id).execute()
    current_mobile = _normalize_mobile(current.data[0].get("mobile") if current.data else "")
    if current_mobile != otp["mobile"]:
        _drop()
        raise HTTPException(status_code=409, detail="מספר הנייד השתנה מאז שליחת הקוד — בקש קוד חדש")

    _drop()
    verified_at = now.isoformat()
    # service_role — the DB trigger blocks anyone else from setting this flag.
    sdb.table("users").update({
        "two_factor_verified": True,
        "two_factor_verified_at": verified_at,
        "updated_at": verified_at,
    }).eq("id", user_id).execute()

    logger.info("2FA verified", extra={"action": "otp_verified", "user_id": user_id})
    return OtpVerifyResponse(
        message="האימות הדו-שלבי הושלם",
        two_factor_verified=True,
        verified_at=verified_at,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Avatar
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/avatar",
    response_model=AvatarUploadResponse,
    summary="Upload my avatar",
    description=(
        "Stores a JPG, PNG, GIF or WebP image of up to 5 MB in GCS and writes "
        "the resulting URL to the user row."
    ),
)
async def upload_avatar(
    file: UploadFile = File(..., description="Image file, 5 MB maximum."),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    user_id = user["uid"]

    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(
            status_code=400,
            detail="סוג קובץ לא נתמך. השתמש ב-JPG, PNG, GIF או WebP",
        )

    contents = await file.read()
    if len(contents) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="הקובץ גדול מדי. מקסימום 5MB")

    ext = AVATAR_EXTENSIONS.get(file.content_type, "jpg")
    avatar_url = upload_to_gcs(
        contents,
        f"{user_id}_{uuid.uuid4().hex[:8]}.{ext}",
        file.content_type,
    )

    try:
        db.table("users").update(
            {"avatar": avatar_url, "updated_at": _now()}
        ).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to store avatar URL: {e}", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="שגיאה בשמירת התמונה")

    logger.info("Avatar uploaded", extra={
        "action": "avatar_upload_success",
        "user_id": user_id,
    })
    return AvatarUploadResponse(message="התמונה הועלתה בהצלחה", avatar_url=avatar_url)


# ══════════════════════════════════════════════════════════════════════════════
# Utility
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/me",
    response_model=MeResponse,
    summary="Describe the current token",
    description="Returns the identity resolved from the bearer token. Useful for debugging auth.",
)
async def get_me(user=Depends(get_current_user)):
    return MeResponse(
        uid=user["uid"],
        email=user.get("email"),
        role=user.get("role"),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Liveness probe for the auth router. No authentication required.",
)
async def health():
    return HealthResponse(status="ok", service="auth", timestamp=_now())
