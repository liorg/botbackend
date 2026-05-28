from fastapi import APIRouter, Depends
from dependencies import get_supabase, get_current_user
from supabase import Client
from pydantic import BaseModel
import uuid, logging
from datetime import datetime, timezone

router = APIRouter(prefix="/webhook-registrations", tags=["webhook_registrations"])
logger = logging.getLogger(__name__)

CALLBACK_BASE = "https://vid.michal-solutions.com/api/webhook-registrations/callback"

_pending: dict[str, list[dict]] = {}

def _key(phone_id: str, contact_id: str) -> str:
    return f"{phone_id}:{contact_id}"

class RegisterRequest(BaseModel):
    phone_id:   str
    contact_id: str

class MessagePayload(BaseModel):
    message_id: str
    phone_id:   str
    contact_id: str
    direction:  bool

@router.post("/register")
async def register_webhook(
    body: RegisterRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    callback_url = f"{CALLBACK_BASE}/{body.phone_id}/{body.contact_id}"
    db.table("webhook_registrations").delete().eq("phone_id", body.phone_id).eq("contact_id", body.contact_id).execute()
    reg_id = str(uuid.uuid4())
    db.table("webhook_registrations").insert({
        "id": reg_id, "phone_id": body.phone_id, "contact_id": body.contact_id,
        "callback_url": callback_url, "type": "recording", "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return {"registration_id": reg_id, "callback_url": callback_url}

@router.delete("/unregister")
async def unregister_webhook(
    body: RegisterRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    db.table("webhook_registrations").delete().eq("phone_id", body.phone_id).eq("contact_id", body.contact_id).execute()
    _pending.pop(_key(body.phone_id, body.contact_id), None)
    return {"ok": True}

@router.post("/callback/{phone_id}/{contact_id}")
async def receive_callback(phone_id: str, contact_id: str, body: MessagePayload):
    k = _key(phone_id, contact_id)
    if k not in _pending:
        _pending[k] = []
    _pending[k].append({"message_id": body.message_id, "phone_id": body.phone_id, "contact_id": body.contact_id, "direction": body.direction})
    logger.info("[CALLBACK] phone=%s contact=%s msg=%s", phone_id, contact_id, body.message_id)
    return {"ok": True}

@router.get("/poll/{phone_id}/{contact_id}")
async def poll_messages(
    phone_id: str, contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    k    = _key(phone_id, contact_id)
    msgs = _pending.pop(k, [])
    if not msgs:
        return {"messages": []}
    ids = [m["message_id"] for m in msgs]
    phone_res    = db.table("phones").select("number").eq("id", phone_id).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")
    result = db.table("messages").select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url").in_("id", ids).order("sent_at").execute()
    from routers.messages import format_message
    return {"messages": [format_message(m, phone_number, phone_id) for m in (result.data or [])]}