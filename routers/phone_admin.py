"""
routers/phone_admin.py — gate for "add phone": allowlist (Beta) + 2FA.

Registration in main.py:
    from routers import phone_admin
    app.include_router(phone_admin.router)

Enforcement on the server — add Depends(require_phone_admin) to every route that creates/assigns a phone.
React's disabled is UX only.
"""

import os

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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


class CanAddPhoneResponse(BaseModel):
    can_add: bool = Field(..., description="True when the user may add a phone (allowlisted and 2FA verified).")
    is_admin: bool = Field(..., description="True when the user's email is in PHONE_ADMINS.")
    two_factor_verified: bool = Field(..., description="True when the user's mobile is verified.")
    reason: Optional[Literal["beta", "2fa"]] = Field(None, description="Why adding is blocked: beta or 2fa. Null when allowed.")
    beta: bool = Field(True, description="Always true while adding phones is in beta.")
    message: Optional[str] = Field(None, description="Message to show when the user is not allowlisted.")
    contact_email: str = Field(..., description="Address to request access.")


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
@router.get("/can_add", response_model=CanAddPhoneResponse, summary="Can I add a phone", description="Tells the UI whether the user may add a phone: allowlisted (beta) and 2FA verified. When not, reason says which check failed.")
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