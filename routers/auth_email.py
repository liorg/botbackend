"""
auth_email.py — FastAPI router
Sign up / Forgot password / Magic link — replaces the Supabase edge functions
(supabase/functions/signup, forgot-password, magic-link).

Flow (all three):
    React → POST /api/auth/email/<action>
          → Supabase admin.generate_link (service_role)
          → action_link embedded in the HTML body (ported 1:1 from the TS files)
          → sent to the user through Resend

Register in main.py:
    from routers import auth_email
    app.include_router(auth_email.router, prefix="/api")

Env:
    RESEND_API_KEY   required
    FROM_EMAIL       default noreply@mg.michal-solutions.com
    APP_NAME         default VID
    SITE_URL         default FRONTEND_URL, then https://ui.michal-solutions.com
    ALLOWED_REDIRECT_ORIGINS  optional, comma separated; SITE_URL is always allowed
"""

import html
import os
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field
from supabase import Client

from dependencies import get_service_supabase
from logging_config import get_logger

logger = get_logger("auth_email")

router = APIRouter(prefix="/auth/email", tags=["auth-email"])

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_URL = "https://api.resend.com/emails"
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@mg.michal-solutions.com")
APP_NAME = os.getenv("APP_NAME", "VID")
SITE_URL = (
    os.getenv("SITE_URL") or os.getenv("FRONTEND_URL") or "https://ui.michal-solutions.com"
).rstrip("/")

SUPPORTED_LANGS = {"he", "en", "ru", "ar"}


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


ALLOWED_ORIGINS = {_origin(SITE_URL)} | {
    _origin(o.strip()) for o in os.getenv("ALLOWED_REDIRECT_ORIGINS", "").split(",") if o.strip()
}


# ══════════════════════════════════════════════════════════════════════════════
# Translations — copied from the edge functions
# ══════════════════════════════════════════════════════════════════════════════

SIGNUP_T = {
    "he": {
        "subject": f"ברוך הבא ל-{APP_NAME}! 🎉",
        "title": "ברוך הבא! 🎉",
        "hello": "שלום",
        "message": f"תודה שנרשמת ל-{APP_NAME}.",
        "clickToVerify": "לחץ על הכפתור כדי לאמת את החשבון שלך:",
        "buttonText": "✓ אמת את החשבון",
        "linkValid": "⏰ הקישור תקף ל-24 שעות",
        "yourEmail": "📧 המייל שלך:",
        "ignoreMessage": "אם לא נרשמת, התעלם מהודעה זו.",
        "regards": f"בברכה, צוות {APP_NAME}",
        "dir": "rtl",
        "emailExists": "אימייל זה כבר רשום במערכת",
        "signupFailed": "שגיאה בהרשמה",
        "linkFailed": "שגיאה ביצירת לינק אימות",
        "emailFailed": "שגיאה בשליחת מייל",
        "success": "נרשמת בהצלחה! בדוק את המייל לאימות החשבון",
    },
    "en": {
        "subject": f"Welcome to {APP_NAME}! 🎉",
        "title": "Welcome! 🎉",
        "hello": "Hello",
        "message": f"Thank you for signing up to {APP_NAME}.",
        "clickToVerify": "Click the button to verify your account:",
        "buttonText": "✓ Verify Account",
        "linkValid": "⏰ This link is valid for 24 hours",
        "yourEmail": "📧 Your email:",
        "ignoreMessage": "If you didn't sign up, please ignore this email.",
        "regards": f"Best regards, {APP_NAME} Team",
        "dir": "ltr",
        "emailExists": "This email is already registered",
        "signupFailed": "Sign up failed",
        "linkFailed": "Failed to generate verification link",
        "emailFailed": "Failed to send email",
        "success": "Successfully registered! Check your email to verify your account",
    },
    "ar": {
        "subject": f"مرحباً بك في {APP_NAME}! 🎉",
        "title": "!مرحباً 🎉",
        "hello": "مرحباً",
        "message": f".{APP_NAME} شكراً لتسجيلك في",
        "clickToVerify": ":انقر على الزر لتأكيد حسابك",
        "buttonText": "✓ تأكيد الحساب",
        "linkValid": "⏰ هذا الرابط صالح لمدة 24 ساعة",
        "yourEmail": ":📧 بريدك الإلكتروني",
        "ignoreMessage": "إذا لم تسجل، يرجى تجاهل هذا البريد.",
        "regards": f"{APP_NAME} مع تحيات فريق",
        "dir": "rtl",
        "emailExists": "هذا البريد الإلكتروني مسجل بالفعل",
        "signupFailed": "فشل التسجيل",
        "linkFailed": "فشل في إنشاء رابط التحقق",
        "emailFailed": "فشل في إرسال البريد الإلكتروني",
        "success": "تم التسجيل بنجاح! تحقق من بريدك الإلكتروني لتأكيد حسابك",
    },
    "ru": {
        "subject": f"Добро пожаловать в {APP_NAME}! 🎉",
        "title": "Добро пожаловать! 🎉",
        "hello": "Здравствуйте",
        "message": f"Спасибо за регистрацию в {APP_NAME}.",
        "clickToVerify": "Нажмите кнопку для подтверждения аккаунта:",
        "buttonText": "✓ Подтвердить аккаунт",
        "linkValid": "⏰ Ссылка действительна 24 часа",
        "yourEmail": "📧 Ваш email:",
        "ignoreMessage": "Если вы не регистрировались, проигнорируйте это письмо.",
        "regards": f"С уважением, команда {APP_NAME}",
        "dir": "ltr",
        "emailExists": "Этот email уже зарегистрирован",
        "signupFailed": "Ошибка регистрации",
        "linkFailed": "Ошибка создания ссылки подтверждения",
        "emailFailed": "Ошибка отправки письма",
        "success": "Регистрация успешна! Проверьте почту для подтверждения аккаунта",
    },
}

RESET_T = {
    "he": {
        "subject": f"איפוס סיסמה - {APP_NAME} 🔐",
        "title": "איפוס סיסמה 🔐",
        "hello": "שלום",
        "message": "קיבלנו בקשה לאיפוס הסיסמה שלך.",
        "clickToReset": "לחץ על הכפתור כדי לבחור סיסמה חדשה:",
        "buttonText": "🔐 איפוס סיסמה",
        "linkValid": "⏰ הקישור תקף ל-60 דקות",
        "yourEmail": "📧 המייל שלך:",
        "ignoreMessage": "אם לא ביקשת לאפס את הסיסמה, התעלם מהודעה זו. הסיסמה שלך לא תשתנה.",
        "regards": f"בברכה, צוות {APP_NAME}",
        "dir": "rtl",
        "linkFailed": "שגיאה ביצירת לינק איפוס",
        "emailFailed": "שגיאה בשליחת מייל",
        "success": "אם האימייל קיים במערכת, נשלח מייל עם הוראות לאיפוס הסיסמה",
    },
    "en": {
        "subject": f"Password Reset - {APP_NAME} 🔐",
        "title": "Password Reset 🔐",
        "hello": "Hello",
        "message": "We received a request to reset your password.",
        "clickToReset": "Click the button to choose a new password:",
        "buttonText": "🔐 Reset Password",
        "linkValid": "⏰ This link is valid for 60 minutes",
        "yourEmail": "📧 Your email:",
        "ignoreMessage": "If you didn't request a password reset, please ignore this email. Your password will not change.",
        "regards": f"Best regards, {APP_NAME} Team",
        "dir": "ltr",
        "linkFailed": "Failed to generate reset link",
        "emailFailed": "Failed to send email",
        "success": "If the email exists in our system, reset instructions have been sent",
    },
    "ar": {
        "subject": f"إعادة تعيين كلمة المرور - {APP_NAME} 🔐",
        "title": "إعادة تعيين كلمة المرور 🔐",
        "hello": "مرحباً",
        "message": "تلقينا طلباً لإعادة تعيين كلمة المرور الخاصة بك.",
        "clickToReset": ":انقر على الزر لاختيار كلمة مرور جديدة",
        "buttonText": "🔐 إعادة تعيين كلمة المرور",
        "linkValid": "⏰ هذا الرابط صالح لمدة 60 دقيقة",
        "yourEmail": ":📧 بريدك الإلكتروني",
        "ignoreMessage": "إذا لم تطلب إعادة تعيين كلمة المرور، يرجى تجاهل هذا البريد. لن تتغير كلمة المرور الخاصة بك.",
        "regards": f"{APP_NAME} مع تحيات فريق",
        "dir": "rtl",
        "linkFailed": "فشل في إنشاء رابط إعادة التعيين",
        "emailFailed": "فشل في إرسال البريد الإلكتروني",
        "success": "إذا كان البريد الإلكتروني موجوداً في نظامنا، فقد تم إرسال تعليمات إعادة التعيين",
    },
    "ru": {
        "subject": f"Сброс пароля - {APP_NAME} 🔐",
        "title": "Сброс пароля 🔐",
        "hello": "Здравствуйте",
        "message": "Мы получили запрос на сброс вашего пароля.",
        "clickToReset": "Нажмите кнопку для выбора нового пароля:",
        "buttonText": "🔐 Сбросить пароль",
        "linkValid": "⏰ Ссылка действительна 60 минут",
        "yourEmail": "📧 Ваш email:",
        "ignoreMessage": "Если вы не запрашивали сброс пароля, проигнорируйте это письмо. Ваш пароль не изменится.",
        "regards": f"С уважением, команда {APP_NAME}",
        "dir": "ltr",
        "linkFailed": "Ошибка создания ссылки сброса",
        "emailFailed": "Ошибка отправки письма",
        "success": "Если email существует в системе, инструкции по сбросу отправлены",
    },
}

MAGIC_T = {
    "he": {
        "welcomeSubject": f"ברוך הבא ל-{APP_NAME}! 🎉",
        "loginSubject": f"התחבר ל-{APP_NAME}",
        "welcomeTitle": "ברוך הבא! 🎉",
        "loginTitle": "התחבר לחשבון שלך 🔐",
        "hello": "שלום",
        "welcomeMessage": f"תודה שנרשמת ל-{APP_NAME}.",
        "loginMessage": "קיבלנו בקשה להתחברות לחשבון שלך.",
        "clickToLogin": "לחץ על הכפתור כדי להתחבר:",
        "buttonTextNew": "🚀 התחבר והתחל",
        "buttonTextExisting": "🔐 התחבר עכשיו",
        "linkValid": "⏰ הקישור תקף ל-60 דקות",
        "yourEmail": "📧 המייל שלך:",
        "ignoreMessage": "אם לא ביקשת להתחבר, התעלם מהודעה זו.",
        "regards": f"בברכה, צוות {APP_NAME}",
        "dir": "rtl",
        "linkFailed": "שגיאה ביצירת לינק התחברות",
        "emailFailed": "שגיאה בשליחת מייל",
        "success": "לינק התחברות נשלח",
    },
    "en": {
        "welcomeSubject": f"Welcome to {APP_NAME}! 🎉",
        "loginSubject": f"Sign in to {APP_NAME}",
        "welcomeTitle": "Welcome! 🎉",
        "loginTitle": "Sign in to your account 🔐",
        "hello": "Hello",
        "welcomeMessage": f"Thank you for signing up to {APP_NAME}.",
        "loginMessage": "We received a request to sign in to your account.",
        "clickToLogin": "Click the button to sign in:",
        "buttonTextNew": "🚀 Sign in & Start",
        "buttonTextExisting": "🔐 Sign in now",
        "linkValid": "⏰ This link is valid for 60 minutes",
        "yourEmail": "📧 Your email:",
        "ignoreMessage": "If you didn't request this, please ignore this email.",
        "regards": f"Best regards, {APP_NAME} Team",
        "dir": "ltr",
        "linkFailed": "Failed to generate magic link",
        "emailFailed": "Failed to send email",
        "success": "Magic link sent",
    },
    "ar": {
        "welcomeSubject": f"مرحباً بك في {APP_NAME}! 🎉",
        "loginSubject": f"تسجيل الدخول إلى {APP_NAME}",
        "welcomeTitle": "!مرحباً 🎉",
        "loginTitle": "تسجيل الدخول إلى حسابك 🔐",
        "hello": "مرحباً",
        "welcomeMessage": f".{APP_NAME} شكراً لتسجيلك في",
        "loginMessage": "تلقينا طلباً لتسجيل الدخول إلى حسابك.",
        "clickToLogin": ":انقر على الزر لتسجيل الدخول",
        "buttonTextNew": "🚀 ابدأ الآن",
        "buttonTextExisting": "🔐 تسجيل الدخول",
        "linkValid": "⏰ هذا الرابط صالح لمدة 60 دقيقة",
        "yourEmail": ":📧 بريدك الإلكتروني",
        "ignoreMessage": "إذا لم تطلب هذا، يرجى تجاهل هذا البريد.",
        "regards": f"{APP_NAME} مع تحيات فريق",
        "dir": "rtl",
        "linkFailed": "فشل في إنشاء رابط تسجيل الدخول",
        "emailFailed": "فشل في إرسال البريد الإلكتروني",
        "success": "تم إرسال رابط تسجيل الدخول",
    },
    "ru": {
        "welcomeSubject": f"Добро пожаловать в {APP_NAME}! 🎉",
        "loginSubject": f"Вход в {APP_NAME}",
        "welcomeTitle": "Добро пожаловать! 🎉",
        "loginTitle": "Войдите в свой аккаунт 🔐",
        "hello": "Здравствуйте",
        "welcomeMessage": f"Спасибо за регистрацию в {APP_NAME}.",
        "loginMessage": "Мы получили запрос на вход в ваш аккаунт.",
        "clickToLogin": "Нажмите кнопку для входа:",
        "buttonTextNew": "🚀 Войти и начать",
        "buttonTextExisting": "🔐 Войти сейчас",
        "linkValid": "⏰ Ссылка действительна 60 минут",
        "yourEmail": "📧 Ваш email:",
        "ignoreMessage": "Если вы не запрашивали это, проигнорируйте это письмо.",
        "regards": f"С уважением, команда {APP_NAME}",
        "dir": "ltr",
        "linkFailed": "Ошибка создания ссылки для входа",
        "emailFailed": "Ошибка отправки письма",
        "success": "Ссылка для входа отправлена",
    },
}


def _t(table: dict, lang: Optional[str]) -> dict:
    return table.get(lang or "he", table["he"])


# ══════════════════════════════════════════════════════════════════════════════
# Email templates — same HTML as the edge functions, values escaped
# ══════════════════════════════════════════════════════════════════════════════

def _layout(t: dict, email: str, link: str, title: str, message: str,
            action_text: str, button_text: str, gradient: str, shadow: str) -> str:
    e = html.escape
    return f"""
    <div dir="{t['dir']}" style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
      <div style="text-align: center; margin-bottom: 30px;">
        <h1 style="color: #3B5BDB; margin: 0;">{e(APP_NAME)}</h1>
      </div>
      <h2 style="color: #333;">{title}</h2>
      <p style="color: #555; font-size: 16px;">{t['hello']},</p>
      <p style="color: #555; font-size: 16px;">{message}</p>
      <p style="color: #555; font-size: 16px;">{action_text}</p>
      <div style="text-align: center; margin: 35px 0;">
        <a href="{e(link, quote=True)}" style="background: {gradient}; color: white; padding: 16px 40px; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 16px; display: inline-block; box-shadow: {shadow};">
          {button_text}
        </a>
      </div>
      <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 25px 0;">
        <p style="color: #666; font-size: 13px; margin: 0;">{t['linkValid']}</p>
        <p style="color: #666; font-size: 13px; margin: 8px 0 0;">{t['yourEmail']} {e(email)}</p>
      </div>
      <p style="color: #999; font-size: 12px;">{t['ignoreMessage']}</p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 25px 0;">
      <p style="color: #999; font-size: 12px; text-align: center;">{t['regards']}</p>
    </div>
    """


def signup_email(email: str, link: str, lang: str) -> tuple[str, str]:
    t = _t(SIGNUP_T, lang)
    return t["subject"], _layout(
        t, email, link, t["title"], t["message"], t["clickToVerify"], t["buttonText"],
        "linear-gradient(135deg, #22c55e 0%, #16a34a 100%)",
        "0 4px 15px rgba(34, 197, 94, 0.3)",
    )


def reset_email(email: str, link: str, lang: str) -> tuple[str, str]:
    t = _t(RESET_T, lang)
    return t["subject"], _layout(
        t, email, link, t["title"], t["message"], t["clickToReset"], t["buttonText"],
        "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)",
        "0 4px 15px rgba(245, 158, 11, 0.3)",
    )


def magic_link_email(email: str, link: str, is_new: bool, lang: str) -> tuple[str, str]:
    t = _t(MAGIC_T, lang)
    return (t["welcomeSubject"] if is_new else t["loginSubject"]), _layout(
        t, email, link,
        t["welcomeTitle"] if is_new else t["loginTitle"],
        t["welcomeMessage"] if is_new else t["loginMessage"],
        t["clickToLogin"],
        t["buttonTextNew"] if is_new else t["buttonTextExisting"],
        "linear-gradient(135deg, #3B5BDB 0%, #228BE6 100%)",
        "0 4px 15px rgba(59, 91, 219, 0.3)",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _db() -> Client:
    try:
        return get_service_supabase()
    except RuntimeError as e:
        logger.error(f"Service client unavailable: {e}")
        raise HTTPException(status_code=503, detail="Auth backend not configured")


def _lang(value: Optional[str]) -> str:
    return value if value in SUPPORTED_LANGS else "he"


def _safe_redirect(url: Optional[str], default: str) -> str:
    """Only redirect back to our own frontend(s)."""
    if url and _origin(url) in ALLOWED_ORIGINS:
        return url
    return default


def _generate_link(db: Client, params: dict):
    """admin.generate_link → (action_link, user). Raises the Supabase error."""
    res = db.auth.admin.generate_link(params)
    props = getattr(res, "properties", None)
    link = getattr(props, "action_link", None) if props else None
    return link, getattr(res, "user", None)


async def _send_resend(to: str, subject: str, body_html: str) -> bool:
    if not RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": FROM_EMAIL, "to": [to], "subject": subject, "html": body_html},
            )
        if r.status_code >= 300:
            logger.error(f"Resend error {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"Resend request failed: {e}")
        return False


def _is_exists_error(e: Exception) -> bool:
    code = str(getattr(e, "code", "") or "").lower()
    msg = str(e).lower()
    return code in ("email_exists", "user_already_exists") or "already" in msg


# ══════════════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════════════

class SignupIn(BaseModel):
    email: EmailStr = Field(..., description="Email address to register.")
    password: str = Field(..., min_length=6, description="Password, at least 6 characters.")
    metadata: dict = Field(default_factory=dict, description="User metadata stored on the Supabase user; full_name is copied to users.name.")
    lang: str = Field("he", description="Email language: he, en, ru or ar.")


class ForgotPasswordIn(BaseModel):
    email: EmailStr = Field(..., description="Email address of the account.")
    redirectTo: Optional[str] = Field(None, description="Page the reset link opens. Only allowed origins are used; otherwise /reset-password.")
    lang: str = Field("he", description="Email language when the user has none stored.")


class MagicLinkIn(BaseModel):
    email: EmailStr = Field(..., description="Email address to sign in with.")
    lang: str = Field("he", description="Email language when the user has none stored.")


class EmailResult(BaseModel):
    success: bool = Field(True, description="True when the email was sent (or, for reset, when the request was accepted).")
    message: str = Field(..., description="Human-readable result in the user's language.")
    user: Optional[dict] = Field(None, description="Created user { id, email }; signup only.")


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/signup", response_model=EmailResult, summary="Sign up — verification email via Resend", description="Creates an unconfirmed user and sends the verification email through Resend in the requested language. Returns 400 when the email is already registered.")
async def signup(body: SignupIn):
    lang = _lang(body.lang)
    t = _t(SIGNUP_T, lang)
    db = _db()

    # generate_link(type=signup) creates the unconfirmed user and returns the
    # confirmation link in one call. An existing email fails here, which
    # replaces the listUsers() scan (that one only saw the first page of users).
    try:
        link, user = _generate_link(db, {
            "type": "signup",
            "email": body.email,
            "password": body.password,
            "options": {
                "data": {**body.metadata, "lang": lang},
                "redirect_to": SITE_URL,
            },
        })
    except Exception as e:
        if _is_exists_error(e):
            raise HTTPException(status_code=400, detail=t["emailExists"])
        logger.error(f"Signup generate_link failed: {e}", extra={"action": "signup_error"})
        raise HTTPException(status_code=400, detail=t["signupFailed"])

    if not link:
        raise HTTPException(status_code=500, detail=t["linkFailed"])

    if user:
        try:
            db.table("users").insert({
                "id": str(user.id),
                "email": user.email,
                "name": body.metadata.get("full_name") or "",
                "lang": lang,
                "package_type": "basic",
            }).execute()
        except Exception as e:
            logger.debug(f"User row insert skipped, a trigger probably created it: {e}")

    subject, body_html = signup_email(body.email, link, lang)
    if not await _send_resend(body.email, subject, body_html):
        raise HTTPException(status_code=500, detail=t["emailFailed"])

    logger.info("Signup email sent", extra={"action": "signup_email_sent", "lang": lang})
    return EmailResult(
        message=t["success"],
        user={"id": str(user.id), "email": user.email} if user else None,
    )


@router.post("/forgot-password", response_model=EmailResult, summary="Password reset email via Resend", description="Sends a password reset email through Resend. Unknown emails get the same success reply, so the endpoint does not reveal which emails exist.")
async def forgot_password(body: ForgotPasswordIn):
    lang = _lang(body.lang)
    t = _t(RESET_T, lang)
    db = _db()

    redirect = _safe_redirect(body.redirectTo, f"{SITE_URL}/reset-password")

    # Unknown email → generate_link fails → same success reply (no enumeration).
    try:
        link, user = _generate_link(db, {
            "type": "recovery",
            "email": body.email,
            "options": {"redirect_to": redirect},
        })
    except Exception as e:
        logger.info(f"Password reset for unknown/invalid email: {e}")
        return EmailResult(message=t["success"])

    if not link:
        raise HTTPException(status_code=500, detail=t["linkFailed"])

    user_lang = _lang(((getattr(user, "user_metadata", None) or {}).get("lang")) or lang)

    subject, body_html = reset_email(body.email, link, user_lang)
    if not await _send_resend(body.email, subject, body_html):
        raise HTTPException(status_code=500, detail=t["emailFailed"])

    logger.info("Reset email sent", extra={"action": "reset_email_sent", "lang": user_lang})
    return EmailResult(message=t["success"])


@router.post("/magic-link", response_model=EmailResult, summary="Magic link email via Resend", description="Sends a sign-in link through Resend. A new email gets a welcome email; an existing user gets a login email in their stored language.")
async def magic_link(body: MagicLinkIn):
    db = _db()

    existing = db.table("users").select("id, lang") \
        .eq("email", body.email).limit(1).execute().data or []
    is_new = not existing
    user_lang = _lang((existing[0].get("lang") if existing else None) or body.lang)
    t = _t(MAGIC_T, user_lang)

    try:
        link, _ = _generate_link(db, {
            "type": "magiclink",
            "email": body.email,
            "options": {"redirect_to": SITE_URL},
        })
    except Exception as e:
        logger.error(f"Magic link generate_link failed: {e}")
        raise HTTPException(status_code=500, detail=t["linkFailed"])

    if not link:
        raise HTTPException(status_code=500, detail=t["linkFailed"])

    subject, body_html = magic_link_email(body.email, link, is_new, user_lang)
    if not await _send_resend(body.email, subject, body_html):
        raise HTTPException(status_code=500, detail=t["emailFailed"])

    logger.info("Magic link sent", extra={
        "action": "magic_link_sent", "new_user": is_new, "lang": user_lang,
    })
    return EmailResult(message=t["success"])
