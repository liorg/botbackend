"""
routers/active_chats.py — כל endpoint = קריאת RPC אחת

main.py:
  from routers import active_chats
  app.include_router(active_chats.router, prefix="/api")
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_supabase
from routers.common_schemas import Passthrough

router = APIRouter(prefix="/active-chats", tags=["active-chats"])


# ── Response Schemas ───────────────────────────────────────────────────────
# Row shapes come from the RPCs; every field they return is kept.

class ActiveContactItem(Passthrough):
    """get_active_contacts row: contact + last call + last message + counts."""


class ActiveMessageItem(Passthrough):
    """get_contact_messages / get_call_messages row."""


class ActiveCallItem(Passthrough):
    """get_contact_calls row."""


class CallLeafItem(Passthrough):
    """get_call_leaves row: leaf + message_ids."""


class ActiveContactsResponse(BaseModel):
    contacts: list[ActiveContactItem] = Field(..., description="Active contacts of the phone, each with last call, last message and counts.")


class ActiveMessagesResponse(BaseModel):
    messages: list[ActiveMessageItem] = Field(..., description="WhatsApp messages, as returned by the RPC.")


class ActiveCallsResponse(BaseModel):
    calls: list[ActiveCallItem] = Field(..., description="Calls of the contact, as returned by the RPC.")


class CallLeavesResponse(BaseModel):
    leaves: list[CallLeafItem] = Field(..., description="Leaves of the call with their message ids.")


@router.get("/{phone_id}/contacts", response_model=ActiveContactsResponse, summary="List active contacts", description="Returns the phone's active contacts with their last call, last message and counts, in one RPC call.")
def get_active_contacts(
    phone_id: str,
    db: Client = Depends(get_supabase),
):
    """אנשי קשר active + last call + last message + counts — RPC אחד."""
    res = db.rpc("get_active_contacts", {"p_phone_id": phone_id}).execute()
    return {"contacts": res.data or []}


@router.get("/{phone_id}/contacts/{contact_id}/messages", response_model=ActiveMessagesResponse, summary="List contact messages", description="Returns the contact's WhatsApp messages on the phone, paged with limit/offset, in one RPC call.")
def get_contact_messages(
    phone_id: str,
    contact_id: str,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Client = Depends(get_supabase),
):
    """הודעות WhatsApp של contact — RPC אחד."""
    res = db.rpc("get_contact_messages", {
        "p_phone_id": phone_id,
        "p_contact_id": contact_id,
        "p_limit": limit,
        "p_offset": offset,
    }).execute()
    return {"messages": res.data or []}


@router.get("/{phone_id}/contacts/{contact_id}/calls", response_model=ActiveCallsResponse, summary="List contact calls", description="Returns the contact's calls on the phone, up to limit, in one RPC call.")
def get_contact_calls(
    phone_id: str,
    contact_id: str,
    limit: int = Query(30, ge=1, le=100),
    db: Client = Depends(get_supabase),
):
    """calls של contact — RPC אחד."""
    res = db.rpc("get_contact_calls", {
        "p_phone_id": phone_id,
        "p_contact_id": contact_id,
        "p_limit": limit,
    }).execute()
    return {"calls": res.data or []}


@router.get("/calls/{call_id}/messages", response_model=ActiveMessagesResponse, summary="List call messages", description="Returns the messages of one call, in one RPC call.")
def get_call_messages(
    call_id: str,
    db: Client = Depends(get_supabase),
):
    """הודעות של call ספציפי — RPC אחד."""
    res = db.rpc("get_call_messages", {"p_call_id": call_id}).execute()
    return {"messages": res.data or []}


@router.get("/calls/{call_id}/leaves", response_model=CallLeavesResponse, summary="List call leaves", description="Returns the leaves of one call with their message ids, in one RPC call.")
def get_call_leaves(
    call_id: str,
    db: Client = Depends(get_supabase),
):
    """leaves + message_ids — RPC אחד."""
    res = db.rpc("get_call_leaves", {"p_call_id": call_id}).execute()
    return {"leaves": res.data or []}