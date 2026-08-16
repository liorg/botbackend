# contact_lang.py — עזרי שפה לאיש קשר + נוסח הודעת הבדיקה (PING)
# מיקום מוצע: לצד dependencies.py
from supabase import Client

DEFAULT_LANG = "he"
SUPPORTED_LANGS = ("he", "en", "ru", "ar")


# ── נוסח הודעת הבדיקה (במקום אימוג'י פעמון) ────────────────────────────────
PING_MESSAGE = {
    "he": "שלום! זוהי הודעת בדיקה לאימות מספר הטלפון שלך. נא להשיב בהודעה כלשהי כדי להשלים את התהליך.",
    "en": "Hello! This is a verification message to confirm your phone number. Please reply with any message to complete the process.",
    "ru": "Здравствуйте! Это проверочное сообщение для подтверждения вашего номера телефона. Пожалуйста, ответьте любым сообщением, чтобы завершить процесс.",
    "ar": "مرحباً! هذه رسالة تحقّق لتأكيد رقم هاتفك. يُرجى الردّ بأي رسالة لإكمال العملية.",
}


def normalize_lang(lang: str | None) -> str:
    """מחזיר שפה נתמכת בלבד."""
    lang = (lang or "").lower().strip()
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def get_ping_message(lang: str | None) -> str:
    """נוסח הודעת הבדיקה לפי שפה."""
    return PING_MESSAGE[normalize_lang(lang)]


def get_user_lang_by_phone(db: Client, phone_id: str) -> str:
    """שפת המשתמש שבבעלותו הטלפון — ברירת המחדל לאיש קשר חדש."""
    try:
        res = (
            db.table("phones")
            .select("user_id, users(lang)")
            .eq("id", phone_id)
            .single()
            .execute()
        )
        return normalize_lang((res.data or {}).get("users", {}).get("lang"))
    except Exception:
        return DEFAULT_LANG


def get_user_lang_by_user_id(db: Client, user_id: str | None) -> str:
    """שפת המשתמש לפי מזהה משתמש."""
    if not user_id:
        return DEFAULT_LANG
    try:
        res = db.table("users").select("lang").eq("id", user_id).single().execute()
        return normalize_lang((res.data or {}).get("lang"))
    except Exception:
        return DEFAULT_LANG


def get_contact_lang(db: Client, contact_id: str) -> str:
    """שפת איש הקשר; אם לא נקבעה — שפת בעל הטלפון."""
    try:
        res = (
            db.table("contacts")
            .select("lang, phone_id")
            .eq("id", contact_id)
            .single()
            .execute()
        )
        row = res.data or {}
        if row.get("lang"):
            return normalize_lang(row["lang"])
        if row.get("phone_id"):
            return get_user_lang_by_phone(db, row["phone_id"])
    except Exception:
        pass
    return DEFAULT_LANG
