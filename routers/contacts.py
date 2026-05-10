# routers/contacts.py
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, timezone

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger

logger = get_logger("contacts")

# ✅ ללא prefix - כי הוא מתווסף ב-main.py
router = APIRouter(tags=["contacts"])

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "secret-token-123")


# ══════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════

class CreateContactFromPingRequest(BaseModel):
    phone_id: str
    target_number: str
    name: Optional[str] = None


class SelectResponseRequest(BaseModel):
    contact_id: str
    message_id: str


class UpdateContactRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    tag: Optional[str] = None
    lid: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

async def _get_agent_ip_for_phone(db: Client, phone_id: str) -> Optional[tuple[str, str]]:
    """Returns (agent_ip, host_id) for a given phone_id"""
    try:
        result = (
            db.table("phones")
            .select("host_id, agent_hosts(ip_address, id)")
            .eq("id", phone_id)
            .execute()
        )
        
        if not result.data:
            return None
        
        phone = result.data[0]
        host = phone.get("agent_hosts")
        
        if not host:
            return None
        
        return (host.get("ip_address"), host.get("id"))
    except Exception as e:
        logger.error(f"Error getting agent IP: {e}")
        return None


def _is_valid_ip(ip: str) -> bool:
    """Validate IP is not localhost/loopback"""
    if not ip:
        return False
    ip_lower = ip.strip().lower()
    blocked = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
    return ip_lower not in blocked and not ip_lower.startswith("127.")


# ══════════════════════════════════════════════════════════════════════
# CRUD Operations
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts")
async def list_contacts(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Get all contacts for a specific phone"""
    logger.info(f"Listing contacts for phone {phone_id}", extra={
        "action": "list_contacts",
        "phone_id": phone_id,
        "user_id": user.get("uid")
    })
    
    try:
        result = (
            db.table("contacts")
            .select("*")
            .eq("phone_id", phone_id)
            .order("created_at", desc=True)
            .execute()
        )
        
        return {"contacts": result.data or []}
    except Exception as e:
        logger.error(f"Error listing contacts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/contacts")
async def create_contact(
    phone_id: str,
    body: dict,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Create a new contact manually"""
    logger.info(f"Creating contact for phone {phone_id}", extra={
        "action": "create_contact",
        "phone_id": phone_id
    })
    
    try:
        contact_data = {
            "phone_id": phone_id,
            "number": body.get("phone", "").replace("+", ""),
            "name": body.get("name"),
            "email": body.get("email"),
            "tag": body.get("tag", "חדש"),
            "lid": body.get("lid"),

        }
        
        result = db.table("contacts").insert(contact_data).execute()
        
        return result.data[0] if result.data else {}
    except Exception as e:
        logger.error(f"Error creating contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: str,
    body: UpdateContactRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Update contact details"""
    logger.info(f"Updating contact {contact_id}", extra={
        "action": "update_contact",
        "contact_id": contact_id
    })
    
    try:
        update_data = {}
        if body.name is not None:
            update_data["name"] = body.name
        if body.email is not None:
            update_data["email"] = body.email
        if body.tag is not None:
            update_data["tag"] = body.tag
        if body.lid is not None:
            update_data["lid"] = body.lid
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        result = db.table("contacts").update(update_data).eq("id", contact_id).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Contact not found")
        
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Delete a contact"""
    logger.info(f"Deleting contact {contact_id}", extra={
        "action": "delete_contact",
        "contact_id": contact_id
    })
    
    try:
        db.table("contacts").delete().eq("id", contact_id).execute()
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error deleting contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 1: Send PING
# ══════════════════════════════════════════════════════════════════════

@router.post("/contacts/create-from-ping")
async def create_contact_from_ping(
    body: CreateContactFromPingRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    Step 1: Create temporary contact and send PING
    
    Flow:
    1. Clean and validate phone number
    2. Check if contact already exists
    3. Create temporary contact (lid=null)
    4. Get agent IP from phone's host
    5. Send PING via .NET Agent
    6. Return contact_id + ping_sender_id for tracking
    """
    logger.info(f"[PING] Creating contact from ping: {body.target_number}", extra={
        "action": "ping_create",
        "phone_id": body.phone_id,
        "target": body.target_number
    })
    
    # Clean phone number
    clean_number = "".join(filter(str.isdigit, body.target_number))
    
    if len(clean_number) < 7 or len(clean_number) > 15:
        raise HTTPException(status_code=400, detail="Invalid phone number length (7-15 digits)")
    
    try:
        # Check if contact exists
        existing = (
            db.table("contacts")
            .select("*")
            .eq("phone_id", body.phone_id)
            .eq("number", clean_number)
            .execute()
        )
        
        if existing.data:
            contact = existing.data[0]
            logger.info(f"[PING] Contact already exists: {contact['id']}")
        else:
            # Create new temporary contact
            contact_data = {
                "phone_id": body.phone_id,
                "number": clean_number,
                "name": body.name or clean_number,
                "lid": None,  # Will be filled after response
                "tag": "חדש",
            }
            
            result = db.table("contacts").insert(contact_data).execute()
            contact = result.data[0]
            logger.info(f"[PING] Created new contact: {contact['id']}")
        
        # Get agent IP
        agent_info = await _get_agent_ip_for_phone(db, body.phone_id)
        
        if not agent_info:
            raise HTTPException(
                status_code=404,
                detail="Agent host not found for this phone. Check if phone has a valid host_id."
            )
        
        agent_ip, host_id = agent_info
        
        if not _is_valid_ip(agent_ip):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid agent IP: {agent_ip} (localhost not allowed in production)"
            )
        
        # Send PING via .NET Agent
        jid = f"{clean_number}@s.whatsapp.net"
        agent_url = f"http://{agent_ip}:5000/api/phones/{body.phone_id}/send/ping"
        
        logger.info(f"[PING] Sending to {agent_url}")
        
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                agent_url,
                json={"jid": jid, "text": "🔔"},
                headers={
                    "X-Agent-Token": AGENT_TOKEN,
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            ping_result = response.json()
        
        logger.info(f"[PING] Success! pingSenderId: {ping_result.get('pingSenderId')}")
        
        return {
            "success": True,
            "contact_id": contact["id"],
            "ping_sender_id": ping_result.get("pingSenderId"),
            "whatsapp_message_id": ping_result.get("messageId"),
            "message": "PING sent successfully. Waiting for response...",
        }
    
    except httpx.HTTPStatusError as e:
        logger.error(f"[PING] Agent HTTP error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Agent returned error: {e.response.text}"
        )
    except httpx.RequestError as e:
        logger.error(f"[PING] Agent unreachable: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach agent at {agent_ip}. Check if .NET service is running."
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PING] Unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 2: Poll for responses
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts/outgoing-with-replies/{phone_id}")
async def get_outgoing_with_replies(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        calls = (
            db.table("calls")
            .select("id, phone_id, contact_id, contacts(id, number, name, lid)")
            .eq("phone_id", phone_id)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )

        conversations = []

        for call in calls.data or []:
            contact = call.get("contacts")
            if not contact:
                continue

            messages = (
                db.table("messages")
                .select("*")
                .eq("call_id", call["id"])
                .gt("sent_at", cutoff)
                .order("sent_at", desc=False)
                .execute()
            )

            msgs = messages.data or []
            if not msgs:
                continue

            last_message = msgs[-1]

            conversations.append({
                "call_id": call["id"],
                "contact": {
                    "id": contact["id"],
                    "name": contact["name"],
                    "number": contact["number"],
                    "lid": contact.get("lid"),
                },
                "last_message": last_message,
                "messages": msgs,
            })

        return {"conversations": conversations}

    except Exception as e:
        logger.error(f"[PING] Error fetching conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 3: Select response and link LID
# ══════════════════════════════════════════════════════════════════════

# בתוך select_response - שלב 3
@router.post("/contacts/select-response")
async def select_response(
    body: SelectResponseRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        # Get the selected message
        message = (
            db.table("messages")
            .select("sender, leaf_id, call_id, content")  # 🔴 הוסף content
            .eq("id", body.message_id)
            .single()
            .execute()
        )
        
        if not message.data:
            raise HTTPException(status_code=404, detail="Message not found")
        
        selected_lid = message.data.get("sender")
        message_content = message.data.get("content", {})
        
        # ✅ שלוף את השם מWhatsApp (אם קיים)
        whatsapp_name = None
        if isinstance(message_content, dict):
            # נסה למצוא שם מהמבנה של WhatsApp message
            whatsapp_name = (
                message_content.get("pushName") or 
                message_content.get("notifyName") or
                message_content.get("verifiedBizName")
            )
        
        if not selected_lid:
            raise HTTPException(
                status_code=400,
                detail="Selected message has no LID"
            )
        
        # ✅ Update contact עם LID + WhatsApp name
        update_data = {
            "lid": selected_lid,
            "tag": "לקוח",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        # רק אם יש whatsapp_name - תעדכן גם אותו
        if whatsapp_name:
            update_data["whatsapp_name"] = whatsapp_name
        
        result = (
            db.table("contacts")
            .update(update_data)
            .eq("id", body.contact_id)
            .execute()
        )
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Contact not found")
        
        logger.info(
            f"[PING] Contact {body.contact_id} linked - "
            f"LID: {selected_lid[:30]}... | "
            f"WhatsApp Name: {whatsapp_name or 'N/A'}"
        )
        
        # ✅ Mark ping_sender as completed
        try:
            db.table("ping_sender").update({
                "status": "completed",
                "matched_contact_id": body.contact_id,
            }).eq("contact_id", body.contact_id).eq("status", "pending").execute()
        except Exception as e:
            logger.warning(f"Failed to update ping_sender: {e}")
        
        return {
            "success": True,
            "message": "Contact linked successfully!",
            "contact": result.data[0],
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PING] Error selecting response: {e}")
        raise HTTPException(status_code=500, detail=str(e))