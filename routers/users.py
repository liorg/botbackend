# routers/users.py
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from typing import Optional
from ..database import get_db  # adjust to your DB session import
from ..models import User       # adjust to your ORM model

router = APIRouter(prefix="/api/users", tags=["users"])


class UserUpdate(BaseModel):
    full_name:    Optional[str]        = None
    email:        Optional[EmailStr]   = None
    mobile:       Optional[str]        = None
    package_type: Optional[str]        = None   # basic | pro | business | enterprise


@router.put("/me")
async def update_me(payload: UserUpdate, db=Depends(get_db), current_user: User = Depends(get_current_user)):
    """Update the current authenticated user's profile."""
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.full_name    is not None: user.full_name    = payload.full_name
    if payload.email        is not None: user.email        = payload.email
    if payload.mobile       is not None: user.mobile       = payload.mobile
    if payload.package_type is not None: user.package_type = payload.package_type

    db.commit()
    db.refresh(user)
    return {
        "id":           user.id,
        "full_name":    user.full_name,
        "email":        user.email,
        "mobile":       user.mobile,
        "package_type": user.package_type,
    }
