"""
routers/phone_admin.py — gate for "add phone": allowlist (Beta) + 2FA.

Registration in main.py:
    from routers import phone_admin
    app.include_router(phone_admin.router)

Enforcement on the server — add Depends(require_phone_admin) to every route that creates/assigns a phone.
React's disabled is UX only.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from dependencies import get_current_user, get_supabase
from routers.two_factor import TWO_FA_REQUIRED, _uid, is_2fa_verified

router = APIRouter(prefix="/phones", tags=["phones"])

# .env:  PHONE_ADMINS=g@michal-solutions.com,other@x.com
_DEFAULT_ADMINS = "g@miichal-solutions.com"

PHONE_ADMINS = {
    e.strip().lower()
    for e in os.getenv("PHONE_ADMINS", _DEFAULT_ADMINS).split(",")
    if e.strip()
}

BETA_MESSAGE = "Beta version. To add a phone, contact contact@grossman.bot"


def _email_of(user) -> str:
    email = getattr(user, "email", None)
    if email is None and isinstance(user, dict):
        email = user.get("email")
    return (email or "").strip().lower()


def is_phone_admin(user) -> bool:
    return _email_of(user) in PHONE_ADMINS


def require_phone_admin(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    Order: allowlist first (403 Beta), then 2FA (403 TWO_FACTOR_REQUIRED).

        @router.post("")
        def create_phone(payload: PhoneCreate, user=Depends(require_phone_admin)):
    """
    if not is_phone_admin(user):
        raise HTTPException(status_code=403, detail=BETA_MESSAGE)
    if not is_2fa_verified(db, _uid(user)):
        raise HTTPException(status_code=403, detail=TWO_FA_REQUIRED)
    return user


# ── must be declared before /phones/{phone_id} in the phones router ────────────
@router.get("/can_add")
def can_add_phone(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    admin    = is_phone_admin(user)
    verified = is_2fa_verified(db, _uid(user))
    return {
        "can_add":             admin and verified,
        "is_admin":            admin,
        "two_factor_verified": verified,
        "reason":              None if admin and verified else ("beta" if not admin else "2fa"),
        "beta":                True,
        "message":             None if admin else BETA_MESSAGE,
        "contact_email":       "contact@grossman.bot",
    }