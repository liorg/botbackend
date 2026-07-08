"""
routers/active_chats.py — כל endpoint = קריאת RPC אחת

main.py:
  from routers import active_chats
  app.include_router(active_chats.router, prefix="/api")
"""

from fastapi import APIRouter, Query
from dependencies import get_supabase

router = APIRouter(prefix="/active-chats", tags=["active-chats"])


@router.get("/{phone_id}/contacts")
def get_active_contacts(phone_id: str):
    """אנשי קשר active + last call + last message + counts — RPC אחד."""
    db = get_supabase()
    res = db.rpc("get_active_contacts", {"p_phone_id": phone_id}).execute()
    return {"contacts": res.data or []}


@router.get("/{phone_id}/contacts/{contact_id}/messages")
def get_contact_messages(
    phone_id: str,
    contact_id: str,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """הודעות WhatsApp של contact — RPC אחד."""
    db = get_supabase()
    res = db.rpc("get_contact_messages", {
        "p_phone_id": phone_id,
        "p_contact_id": contact_id,
        "p_limit": limit,
        "p_offset": offset,
    }).execute()
    return {"messages": res.data or []}


@router.get("/{phone_id}/contacts/{contact_id}/calls")
def get_contact_calls(
    phone_id: str,
    contact_id: str,
    limit: int = Query(30, ge=1, le=100),
):
    """calls של contact — RPC אחד."""
    db = get_supabase()
    res = db.rpc("get_contact_calls", {
        "p_phone_id": phone_id,
        "p_contact_id": contact_id,
        "p_limit": limit,
    }).execute()
    return {"calls": res.data or []}


@router.get("/calls/{call_id}/messages")
def get_call_messages(call_id: str):
    """הודעות של call ספציפי — RPC אחד."""
    db = get_supabase()
    res = db.rpc("get_call_messages", {"p_call_id": call_id}).execute()
    return {"messages": res.data or []}


@router.get("/calls/{call_id}/leaves")
def get_call_leaves(call_id: str):
    """leaves + message_ids — RPC אחד."""
    db = get_supabase()
    res = db.rpc("get_call_leaves", {"p_call_id": call_id}).execute()
    return {"leaves": res.data or []}