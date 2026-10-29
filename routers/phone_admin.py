"""
routers/phone_admin.py — gate for "add phone": admin OR 2FA.

Registration in main.py:
    from routers import phone_admin
    app.include_router(phone_admin.router)

Register this router before routes such as /phones/{phone_id}.

Enforcement on the server:
    Add Depends(require_phone_admin) to every route that creates/assigns a phone.
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

# .env: PHONE_ADMINS=g@miichal-solutions.com,other@x.com
_DEFAULT_ADMINS = "g@miichal-solutions.com"

PHONE_ADMINS = {
    e.strip().lower()
    for e in os.getenv("PHONE_ADMINS", _DEFAULT_ADMINS).split(",")
    if e.strip()
}


class CanAddPhoneResponse(BaseModel):
    can_add: bool = Field(
        ...,
        description="True when the user is allowlisted OR 2FA verified.",
    )
    is_admin: bool = Field(
        ...,
        description="True when the user's email is in PHONE_ADMINS.",
    )
    two_factor_verified: bool = Field(
        ...,
        description="True when the user's mobile is verified.",
    )
    reason: Optional[Literal["beta", "2fa"]] = Field(
        None,
        description="2fa when blocked. Null when allowed.",
    )
    beta: bool = Field(
        False,
        description="False: adding phones is not restricted to beta admins.",
    )
    message: Optional[str] = Field(
        None,
        description="Optional message to display.",
    )
    contact_email: str = Field(
        ...,
        description="Support contact address.",
    )


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
    Allow admins without 2FA.
    Other users must have a verified mobile.

    Usage:
        @router.post("")
        def create_phone(
            payload: PhoneCreate,
            user=Depends(require_phone_admin),
        ):
            ...
    """
    if is_phone_admin(user):
        return user

    if not is_2fa_verified(db, _uid(user)):
        raise HTTPException(
            status_code=403,
            detail=TWO_FA_REQUIRED,
        )

    return user


@router.get(
    "/can_add",
    response_model=CanAddPhoneResponse,
    summary="Can I add a phone",
    description=(
        "Tells the UI whether the user may add a phone: "
        "allowlisted admin OR 2FA verified. "
        "When blocked, reason is 2fa."
    ),
)
def can_add_phone(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    admin = is_phone_admin(user)
    verified = is_2fa_verified(db, _uid(user))
    allowed = admin or verified

    return {
        "can_add": allowed,
        "is_admin": admin,
        "two_factor_verified": verified,
        "reason": None if allowed else "2fa",
        "beta": False,
        "message": None,
        "contact_email": "contact@grossman.bot",
    }
