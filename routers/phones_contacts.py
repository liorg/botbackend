from fastapi import APIRouter, Depends
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/phones/{phone_id}/contacts", tags=["phone-contacts"])


# GET /phones/{phone_id}/contacts/active
# SELECT id, name, number, avatar, is_bot FROM contacts
# WHERE phone_id = :phone_id AND tag = 'active'
# ORDER BY name
@router.get("/active")
async def list_active_contacts(phone_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("contacts")
        .select("id, name, number, avatar, is_bot")
        .eq("phone_id", phone_id)
        .eq("tag", "active")
        .order("name")
        .execute()
    )
    return result.data or []