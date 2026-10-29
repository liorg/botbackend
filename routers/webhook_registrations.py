import logging
import threading
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from supabase import Client

from dependencies import get_current_user, get_supabase
from routers.messages import ChatMessage


router = APIRouter(
    prefix="/webhook-registrations",
    tags=["webhook_registrations"],
)

logger = logging.getLogger(__name__)

# זמני בזיכרון.
# מתאים רק ל-instance יחיד של FastAPI.
# def endpoints run in the threadpool: every access goes through the lock,
# otherwise an append can land in a list poll has already popped.
_pending: dict[str, list[dict]] = {}
_pending_lock = threading.Lock()


def _key(phone_id: str, contact_id: str) -> str:
    return f"{phone_id}:{contact_id}"


class MessagePayload(BaseModel):
    """Payload from the HostAgent, camelCase:

    {
        "messageId": "...",
        "whatsAppMessageId": "...",
        "phoneId": "...",
        "contactId": "...",
        "direction": true
    }
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )

    # messages.id
    message_id: str = Field(
        default="",
        alias="messageId", description="messages.id of the stored message.",
    )

    # מזהה WhatsApp/Baileys
    whatsapp_message_id: str = Field(
        default="",
        alias="whatsAppMessageId", description="WhatsApp / Baileys message id.",
    )

    phone_id: str = Field(
        default="",
        alias="phoneId", description="Phone id.",
    )

    contact_id: str = Field(
        default="",
        alias="contactId", description="Contact id.",
    )

    # true = incoming, false = outgoing
    direction: bool = Field(
        default=False,
        alias="direction", description="True = incoming, false = outgoing.",
    )


class CallbackResponse(BaseModel):
    ok: bool = Field(..., description="False when phoneId or contactId is missing.")
    error: Optional[str] = Field(None, description="Set when ok is false.")
    message_id: Optional[str] = Field(None, description="messages.id from the payload.")
    whatsapp_message_id: Optional[str] = Field(None, description="WhatsApp message id from the payload.")
    call_id: Optional[str] = Field(None, description="Active recording call the message was linked to.")


class PollResponse(BaseModel):
    messages: list[ChatMessage] = Field(..., description="Messages received since the last poll, formatted for the chat view.")


def _resolve_recording_call_id(
    db: Client,
    phone_id: str,
    contact_id: str,
) -> Optional[str]:
    """
    מחפש רק recording call פעיל.

    בפרויקט backend של recording הסטטוס הפעיל הוא active,
    ולא running של Worker Scenario Runtime.
    """

    result = (
        db.table("calls")
        .select("id")
        .eq("phone_id", phone_id)
        .eq("contact_id", contact_id)
        .eq("call_type", "recording")
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]["id"]

    # תמיכה במקרה שהודעה שייכת ל-child contact,
    # אבל recording call נפתח על parent contact.
    contact_result = (
        db.table("contacts")
        .select("parent_contact_id")
        .eq("id", contact_id)
        .limit(1)
        .execute()
    )

    parent_contact_id = (
        contact_result.data or [{}]
    )[0].get("parent_contact_id")

    if not parent_contact_id:
        return None

    parent_result = (
        db.table("calls")
        .select("id")
        .eq("phone_id", phone_id)
        .eq("contact_id", parent_contact_id)
        .eq("call_type", "recording")
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    return (
        parent_result.data[0]["id"]
        if parent_result.data
        else None
    )


@router.post("/callback", response_model=CallbackResponse, summary="Recording callback", description="Called by the HostAgent after it stores a message. Links the message to the active recording call (also through the parent contact) and queues it for polling.")
def receive_callback(
    body: MessagePayload,
    db: Client = Depends(get_supabase),
):
    """
    Callback גלובלי מסוג recording.

    ה-HostAgent כבר שמר את ההודעה בטבלת messages.

    ה-backend:
      1. מאתר recording call פעיל.
      2. מקשר messages.call_id.
      3. שומר pending לצורך polling.
    """

    phone_id = body.phone_id
    contact_id = body.contact_id
    message_id = body.message_id
    whatsapp_message_id = body.whatsapp_message_id

    if not phone_id or not contact_id:
        logger.warning(
            "[RECORDING CALLBACK] missing phone/contact | "
            "phone=%s contact=%s message=%s whatsapp=%s",
            phone_id,
            contact_id,
            message_id,
            whatsapp_message_id,
        )

        return {
            "ok": False,
            "error": "phoneId and contactId are required",
        }

    call_id = _resolve_recording_call_id(
        db,
        phone_id,
        contact_id,
    )

    if call_id and message_id:
        try:
            update_result = (
                db.table("messages")
                .update(
                    {
                        "call_id": call_id,
                    }
                )
                .eq("id", message_id)
                .eq("phone_id", phone_id)
                .eq("contact_id", contact_id)
                .execute()
            )

            if update_result.data:
                logger.info(
                    "[RECORDING CALLBACK] message=%s whatsapp=%s "
                    "call=%s direction=%s",
                    message_id,
                    whatsapp_message_id,
                    call_id,
                    "incoming" if body.direction else "outgoing",
                )
            else:
                logger.warning(
                    "[RECORDING CALLBACK] message not found | "
                    "message=%s phone=%s contact=%s",
                    message_id,
                    phone_id,
                    contact_id,
                )

        except Exception:
            logger.exception(
                "[RECORDING CALLBACK] failed updating message | "
                "message=%s whatsapp=%s call=%s",
                message_id,
                whatsapp_message_id,
                call_id,
            )

    with _pending_lock:
        _pending.setdefault(
            _key(phone_id, contact_id),
            [],
        ).append(
            {
                "message_id": message_id,
                "whatsapp_message_id": whatsapp_message_id,
                "phone_id": phone_id,
                "contact_id": contact_id,
                "direction": body.direction,
                "call_id": call_id,
            }
        )

    return {
        "ok": True,
        "message_id": message_id,
        "whatsapp_message_id": whatsapp_message_id,
        "call_id": call_id,
    }


@router.get("/poll/{phone_id}/{contact_id}", response_model=PollResponse, summary="Poll recording messages", description="Returns the messages queued for the phone and contact since the last poll, formatted for the chat view. Each message is returned once.")
def poll_messages(
    phone_id: str,
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    with _pending_lock:
        pending_messages = _pending.pop(
            _key(phone_id, contact_id),
            [],
        )

    if not pending_messages:
        return {
            "messages": [],
        }

    message_ids = [
        item["message_id"]
        for item in pending_messages
        if item.get("message_id")
    ]

    if not message_ids:
        return {
            "messages": [],
        }

    phone_result = (
        db.table("phones")
        .select("number")
        .eq("id", phone_id)
        .limit(1)
        .execute()
    )

    phone_number = (
        phone_result.data or [{}]
    )[0].get("number", "")

    message_result = (
        db.table("messages")
        .select(
            "id, whatsapp_message_id, contact_id, phone_id, "
            "sender, content, sent_at, direction, media_url, call_id"
        )
        .in_("id", message_ids)
        .order("sent_at")
        .execute()
    )

    from routers.messages import format_message

    return {
        "messages": [
            format_message(
                message,
                phone_number,
                phone_id,
            )
            for message in (message_result.data or [])
        ],
    }