# contacts.py
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta, timezone

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger

logger = get_logger("contacts")
router = APIRouter(prefix="/contacts", tags=["contacts"])

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")


# ── Models ────────────────────────────────────────────────────────────
class CreateContactFromPingRequest(BaseModel):
    phone_id: str
    target_number: str
    name: Optional[str] = None


class SelectResponseRequest(BaseModel):
    contact_id: str
    message_id: str  # ההודעה שהמשתמש בחר


# ── Helpers ───────────────────────────────────────────────────────────
async def _get_agent_ip_for_phone(db: Client, phone_id: str) -> Optional[str]:
    result = (
        db.table("phones")
        .select("agent_hosts(ip_address)")
        .eq("id", phone_id)
        .execute()
    )
    
    if not result.data:
        return None
    
    host = result.data[0].get("agent_hosts")
    return host.get("ip_address") if host else None


# ══════════════════════════════════════════════════════════════════════
# Step 1: שליחת PING
# ══════════════════════════════════════════════════════════════════════
@router.post("/create-from-ping")
async def create_contact_from_ping(
    body: CreateContactFromPingRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    שלב 1: יצירת contact זמני + שליחת PING
    """
    logger.info(f"[PING] Creating contact for {body.target_number}")

    clean_number = "".join(filter(str.isdigit, body.target_number))
    
    if len(clean_number) < 7:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    # יצירת contact זמני
    contact_data = {
        "phone_id": body.phone_id,
        "number": clean_number,
        "name": body.name or clean_number,
        "lid": None,
        "tag": "חדש",
        "status": "pending_ping",
    }
    
    result = db.table("contacts").insert(contact_data).execute()
    contact = result.data[0]

    # שליחת PING דרך Agent
    agent_ip = await _get_agent_ip_for_phone(db, body.phone_id)
    if not agent_ip:
        raise HTTPException(status_code=404, detail="Agent not found")

    jid = f"{clean_number}@s.whatsapp.net"
    agent_url = f"http://{agent_ip}:5000/api/phones/{body.phone_id}/send/ping"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                agent_url,
                json={"jid": jid, "text": "🔔"},
                headers={"X-Agent-Token": AGENT_TOKEN},
            )
            response.raise_for_status()
            ping_result = response.json()

        return {
            "success": True,
            "contact_id": contact["id"],
            "ping_sender_id": ping_result.get("pingSenderId"),
            "whatsapp_message_id": ping_result.get("messageId"),
        }

    except Exception as e:
        logger.error(f"[PING] Error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# Step 2: שליפת הודעות יוצאות + התגובות שלהן
# ══════════════════════════════════════════════════════════════════════
@router.get("/outgoing-with-replies/{phone_id}")
async def get_outgoing_with_replies(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    מחזיר את כל ההודעות שנשלחו + התגובות שהתקבלו
    
    Structure:
    [
      {
        "sent_message": {...},  # ההודעה שנשלחה
        "replies": [...]        # התגובות שהתקבלו
      }
    ]
    """
    # שליפת הודעות יוצאות (24 שעות אחרונות)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    outgoing = (
        db.table("messages")
        .select("*, contacts(id, number, name, lid)")
        .eq("call_id", phone_id)
        .eq("direction", False)  # יוצאות
        .gt("sent_at", cutoff)
        .order("sent_at", desc=True)
        .limit(50)
        .execute()
    )

    # עבור כל הודעה יוצאת, שלוף תגובות
    conversations = []
    
    for msg in outgoing.data:
        contact = msg.get("contacts")
        
        if not contact:
            continue

        # שליפת תגובות (הודעות נכנסות מאותו contact)
        replies = (
            db.table("messages")
            .select("*")
            .eq("call_id", phone_id)
            .eq("contact_id", contact["id"])
            .eq("direction", True)  # נכנסות
            .gt("sent_at", msg["sent_at"])  # רק תגובות אחרי ההודעה שנשלחה
            .order("sent_at", asc=True)
            .limit(10)
            .execute()
        )

        conversations.append({
            "sent_message": {
                "id": msg["id"],
                "content": msg["content"],
                "sent_at": msg["sent_at"],
                "contact_number": contact["number"],
                "contact_name": contact["name"],
            },
            "replies": [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "sender": r.get("sender"),  # 🔴 זה ה-LID!
                    "sent_at": r["sent_at"],
                    "leaf_id": r.get("leaf_id"),
                }
                for r in (replies.data or [])
            ],
        })

    return {"conversations": conversations}


# ══════════════════════════════════════════════════════════════════════
# Step 3: בחירת תגובה ועדכון Contact
# ══════════════════════════════════════════════════════════════════════
@router.post("/select-response")
async def select_response(
    body: SelectResponseRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    משתמש בחר תגובה מסוימת → מעדכן את ה-contact עם ה-LID שלה
    """
    logger.info(f"[PING] Selecting response {body.message_id} for contact {body.contact_id}")

    # שליפת ההודעה הנבחרת
    message = (
        db.table("messages")
        .select("sender, leaf_id")
        .eq("id", body.message_id)
        .single()
        .execute()
    )

    if not message.data:
        raise HTTPException(status_code=404, detail="Message not found")

    selected_lid = message.data.get("sender")
    
    if not selected_lid:
        raise HTTPException(status_code=400, detail="No LID in selected message")

    # עדכון ה-contact
    result = (
        db.table("contacts")
        .update({
            "lid": selected_lid,
            "status": "active",
            "tag": "לקוח",
        })
        .eq("id", body.contact_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Contact not found")

    logger.info(f"[PING] Contact {body.contact_id} linked with LID {selected_lid}")

    return {
        "success": True,
        "message": "Contact updated successfully!",
        "contact": result.data[0],
    }


# ══════════════════════════════════════════════════════════════════════
# CRUD רגיל
# ══════════════════════════════════════════════════════════════════════
@router.get("/")
async def list_contacts(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("contacts")
        .select("*")
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .execute()
    )
    
    return {"contacts": result.data or []}