from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/phones/{phone_id}/contacts", tags=["phone-contacts"])


class ActiveContactResponse(BaseModel):
    id: str = Field(..., description="Contact id.")
    name: Optional[str] = Field(None, description="Contact name.")
    number: Optional[str] = Field(None, description="Phone number.")
    avatar: Optional[str] = Field(None, description="Avatar URL.")
    is_bot: Optional[bool] = Field(None, description="True when the contact is a bot.")


# GET /phones/{phone_id}/contacts/active
# SELECT id, name, number, avatar, is_bot FROM contacts
# WHERE phone_id = :phone_id AND tag = 'active'
# ORDER BY name
@router.get("/active", response_model=list[ActiveContactResponse], summary="List ACTIVE contacts", description="Returns the phone's contacts whose tag is 'active', ordered by name: id, name, number, avatar, is_bot.")
def list_active_contacts(phone_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("contacts")
        .select("id, name, number, avatar, is_bot")
        .eq("phone_id", phone_id)
        .eq("tag", "active")
        .order("name")
        .execute()
    )
    return result.data or []