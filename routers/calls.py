from fastapi import APIRouter, Depends
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/calls", tags=["calls"])

@router.get("/phone/{phone_id}")
async def list_calls(phone_id: str, db: Client = Depends(get_supabase)):
    result = db.table("calls")\
        .select("*, contacts(name, number), scenarios(name)")\
        .eq("phone_id", phone_id)\
        .order("created_at", desc=True)\
        .execute()
    return result.data

@router.post("/")
async def create_call(body: dict, db: Client = Depends(get_supabase)):
    result = db.table("calls").insert(body).execute()
    return result.data[0]

@router.patch("/{call_id}")
async def update_call(call_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("calls").update(body).eq("id", call_id).execute()
    return result.data[0]

# ── Messages ──────────────────────────────────────────────────

@router.get("/{call_id}/messages")
async def list_messages(call_id: str, db: Client = Depends(get_supabase)):
    result = db.table("messages")\
        .select("*")\
        .eq("call_id", call_id)\
        .order("sent_at")\
        .execute()
    return result.data

@router.post("/{call_id}/messages")
async def add_message(call_id: str, body: dict, db: Client = Depends(get_supabase)):
    # body = { "sender": "bot"|"test", "content": {...} }
    result = db.table("messages").insert({
        "call_id": call_id,
        "sender":  body["sender"],
        "content": body["content"],
    }).execute()
    return result.data[0]
