from fastapi import APIRouter, Depends
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/contacts", tags=["contacts"])

@router.get("/phone/{phone_id}")
async def list_contacts(phone_id: str, db: Client = Depends(get_supabase)):
    result = db.table("contacts").select("*").eq("phone_id", phone_id).execute()
    return result.data

@router.get("/phone/{phone_id}/bots")
async def list_bots(phone_id: str, db: Client = Depends(get_supabase)):
    result = db.table("contacts").select("*").eq("phone_id", phone_id).eq("is_bot", True).execute()
    return result.data

@router.post("/")
async def create_contact(body: dict, db: Client = Depends(get_supabase)):
    result = db.table("contacts").insert(body).execute()
    return result.data[0]

@router.patch("/{contact_id}")
async def update_contact(contact_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("contacts").update(body).eq("id", contact_id).execute()
    return result.data[0]

@router.delete("/{contact_id}")
async def delete_contact(contact_id: str, db: Client = Depends(get_supabase)):
    db.table("contacts").delete().eq("id", contact_id).execute()
    return {"ok": True}
