# routers/common_schemas.py — response models shared by several routers.
# Module only (no APIRouter) — do not register it in main.py.
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class Passthrough(BaseModel):
    """Base for payloads whose full shape lives outside this code
    (Supabase RPC, agent / worker replies, select("*") rows).
    Declared fields are documented and type-checked; undeclared keys are kept as-is."""
    model_config = ConfigDict(extra="allow")


class OkResponse(BaseModel):
    ok: bool = Field(True, description="True when the operation succeeded.")


class ContactRow(Passthrough):
    """A contacts row. Extra columns from select("*") are kept."""
    id: str = Field(..., description="Contact id.")
    phone_id: Optional[str] = Field(None, description="Phone the contact belongs to.")
    user_id: Optional[str] = Field(None, description="Owner of the phone.")
    number: Optional[str] = Field(None, description="Phone number, digits only.")
    name: Optional[str] = Field(None, description="Contact name.")
    whatsapp_name: Optional[str] = Field(None, description="Name shown on WhatsApp (pushName).")
    email: Optional[str] = Field(None, description="Contact email.")
    tag: Optional[str] = Field(None, description="new, draft, active or ignore.")
    lid: Optional[str] = Field(None, description="WhatsApp LID of the contact.")
    lang: Optional[str] = Field(None, description="Messaging language: he, en, ru or ar.")
    is_connect: Optional[bool] = Field(None, description="True when a draft was connected to a parent contact.")
    parent_contact_id: Optional[str] = Field(None, description="Contact this draft is linked to.")
    created_at: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")
    updated_at: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")


class MessageRow(Passthrough):
    """A messages row as stored. content is JSON (object or string)."""
    id: str = Field(..., description="Message id (messages.id).")
    contact_id: Optional[str] = Field(None, description="Contact id.")
    phone_id: Optional[str] = Field(None, description="Phone id.")
    call_id: Optional[str] = Field(None, description="Recording call the message is linked to.")
    whatsapp_message_id: Optional[str] = Field(None, description="WhatsApp / Baileys message id.")
    sender: Optional[str] = Field(None, description="Sender number or LID.")
    content: Any = Field(None, description="Message content: JSON object or raw string.")
    status: Any = Field(None, description="Delivery status as stored by the agent.")          # Baileys may store a number here
    sent_at: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")
    direction: Optional[bool] = Field(None, description="True = incoming, false = outgoing.")
    media_url: Optional[str] = Field(None, description="Agent media URL, when the message has media.")
