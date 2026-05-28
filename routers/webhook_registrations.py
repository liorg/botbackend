from fastapi import APIRouter, Depends
from dependencies import get_supabase, get_current_user
from supabase import Client
from pydantic import BaseModel, Field
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
    MessageId:  str = Field(alias="MessageId", default="")
    PhoneId:    str = Field(alias="PhoneId",   default="")
    ContactId:  str = Field(alias="ContactId", default="")
    Direction:  bool = Field(alias="Direction", default=False)

    model_config = {"populate_by_name": True}


async def _resolve_call_id(db: Client, phone_id: str, contact_id: str) -> str | None:
    res = (
        db.table("calls")
        .select("id")
        .eq("phone_id",   phone_id)
        .eq("contact_id", contact_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]["id"]

    contact_res = (
        db.table("contacts")
        .select("parent_contact_id")
        .eq("id", contact_id)
        .limit(1)
        .execute()
    )
    parent_id = (contact_res.data or [{}])[0].get("parent_contact_id")
    if not parent_id:
        return None

    res2 = (
        db.table("calls")
        .select("id")
        .eq("phone_id",   phone_id)
        .eq("contact_id", parent_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res2.data[0]["id"] if res2.data else None


@router.post("/register")
async def register_webhook(
    body: RegisterRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    callback_url = f"{CALLBACK_BASE}/{body.phone_id}/{body.contact_id}"
    db.table("webhook_registrations").delete() \
      .eq("phone_id",   body.phone_id) \
      .eq("contact_id", body.contact_id).execute()
    db.table("webhook_registrations").insert({
        "id":           str(uuid.uuid4()),
        "phone_id":     body.phone_id,
        "contact_id":   body.contact_id,
        "callback_url": callback_url,
        "type":         "recording",
        "is_active":    True,
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }).execute()
    return {"callback_url": callback_url}


@router.delete("/unregister")
async def unregister_webhook(
    body: RegisterRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    db.table("webhook_registrations").delete() \
      .eq("phone_id",   body.phone_id) \
      .eq("contact_id", body.contact_id).execute()
    _pending.pop(_key(body.phone_id, body.contact_id), None)
    return {"ok": True}


@router.post("/callback/{phone_id}/{contact_id}")
async def receive_callback(
    phone_id:   str,
    contact_id: str,
    body: MessagePayload,
    db: Client = Depends(get_supabase),
):
    message_id = body.MessageId
    call_id    = await _resolve_call_id(db, phone_id, contact_id)

    if call_id and message_id:
        try:
            db.table("messages") \
              .update({"call_id": call_id}) \
              .eq("id", message_id) \
              .execute()
            logger.info("[CALLBACK] msg=%s → call=%s", message_id, call_id)
        except Exception as e:
            logger.warning("[CALLBACK] Failed to update call_id: %s", e)

    k = _key(phone_id, contact_id)
    _pending.setdefault(k, []).append({
        "message_id": message_id,
        "phone_id":   phone_id,
        "contact_id": contact_id,
        "direction":  body.Direction,
        "call_id":    call_id,
    })
    return {"ok": True, "call_id": call_id}


@router.get("/poll/{phone_id}/{contact_id}")
async def poll_messages(
    phone_id:   str,
    contact_id: str,
    user=Depends(get_current_user),
    db: Client  = Depends(get_supabase),
):
    k    = _key(phone_id, contact_id)
    msgs = _pending.pop(k, [])
    if not msgs:
        return {"messages": []}

    ids          = [m["message_id"] for m in msgs if m.get("message_id")]
    phone_res    = db.table("phones").select("number").eq("id", phone_id).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    result = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .in_("id", ids)
        .order("sent_at")
        .execute()
    )

    from routers.messages import format_message
    return {"messages": [format_message(m, phone_number, phone_id) for m in (result.data or [])]}
