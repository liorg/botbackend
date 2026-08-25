"""
routers/phone_admin.py — שער הרשאה ל"הוספת טלפון" (Beta gate)

רישום ב-main.py:
    from routers import phone_admin
    app.include_router(phone_admin.router)

חשוב: ה-disabled בצד React הוא רק UX. האכיפה האמיתית היא כאן —
מוסיפים Depends(require_phone_admin) לכל route שיוצר/מקצה טלפון.
"""

import os
from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_current_user

router = APIRouter(prefix="/phones", tags=["phones"])

# רשימה מופרדת בפסיקים ב-.env:  PHONE_ADMINS=g@michal-solutions.com,other@x.com
_DEFAULT_ADMINS = "g@miichal-solutions.com"

PHONE_ADMINS = {
    e.strip().lower()
    for e in os.getenv("PHONE_ADMINS", _DEFAULT_ADMINS).split(",")
    if e.strip()
}

BETA_MESSAGE = "Beta version. To add a phone, contact contact@grossman.bot"


def _email_of(user) -> str:
    """get_current_user מחזיר לפעמים אובייקט ולפעמים dict — תומך בשניהם."""
    email = getattr(user, "email", None)
    if email is None and isinstance(user, dict):
        email = user.get("email")
    return (email or "").strip().lower()


def is_phone_admin(user) -> bool:
    return _email_of(user) in PHONE_ADMINS


def require_phone_admin(user=Depends(get_current_user)):
    """
    Dependency לחסימה. שימוש בראוטר הטלפונים הקיים:

        @router.post("")
        async def create_phone(
            payload: PhoneCreate,
            db: Client = Depends(get_supabase),
            user=Depends(require_phone_admin),   # ← במקום get_current_user
        ):
            ...
    """
    if not is_phone_admin(user):
        raise HTTPException(status_code=403, detail=BETA_MESSAGE)
    return user


# ── חייב להיות מוצהר לפני /phones/{phone_id} בראוטר הטלפונים ──────────────
@router.get("/can_add")
async def can_add_phone(user=Depends(get_current_user)):
    """מקור אמת יחיד ל-UI — האם להציג את כפתור ההוספה כפעיל."""
    allowed = is_phone_admin(user)
    return {
        "can_add": allowed,
        "beta": True,
        "message": None if allowed else BETA_MESSAGE,
        "contact_email": "contact@grossman.bot",
    }
