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

router = APIRouter(tags=["contacts"])

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "secret-token-123")


# ══════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════

class CreateContactFromPingRequest(BaseModel):
    phone_id: str
    target_number: str
    name: Optional[str] = None
    override_contact_id: Optional[str] = None   # ← לדריסת contact קיים (OVERRIDE mode)


class SelectResponseRequest(BaseModel):
    contact_id: str
    message_id: str


class UpdateContactRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    tag: Optional[str] = None
    lid: Optional[str] = None


class CheckPhoneResponse(BaseModel):
    status: str                        # "new" | "override" | "blocked"
    contact_id: Optional[str] = None
    contact_name: Optional[str] = None
    contact_number: Optional[str] = None
    ping_step: Optional[str] = None   # "pending" | "waiting_reply" | None
    ping_sender_id: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

async def _get_agent_ip_for_phone(db: Client, phone_id: str) -> Optional[tuple[str, str]]:
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
    if not ip:
        return False
    ip_lower = ip.strip().lower()
    blocked = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
    return ip_lower not in blocked and not ip_lower.startswith("127.")


# ══════════════════════════════════════════════════════════════════════
# check-phone — onBlur check
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts/check-phone", response_model=CheckPhoneResponse)
async def check_phone(
    phone_id: str,
    number: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    clean = "".join(filter(str.isdigit, number))
    if not clean or len(clean) < 7:
        return CheckPhoneResponse(status="new")

    logger.info(f"[check-phone] phone_id={phone_id} number={clean}")

    try:
        contact_res = (
            db.table("contacts")
            .select("id, name, number, lid, whatsapp_name")
            .eq("phone_id", phone_id)
            .eq("number", clean)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error(f"[check-phone] contacts query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not contact_res.data:
        return CheckPhoneResponse(status="new")

    contact = contact_res.data[0]
    display_name = (
        contact.get("whatsapp_name") or
        contact.get("name") or
        contact.get("number")
    )

    if contact.get("lid"):
        return CheckPhoneResponse(
            status="blocked",
            contact_id=contact["id"],
            contact_name=display_name,
            contact_number=contact["number"],
        )

    ping_step = None
    ping_sender_id = None
    try:
        ping_res = (
            db.table("ping_sender")
            .select("id, status")
            .eq("contact_id", contact["id"])
            .in_("status", ["pending", "waiting_reply"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if ping_res.data:
            row = ping_res.data[0]
            ping_step      = row["status"]
            ping_sender_id = row["id"]
    except Exception as e:
        logger.warning(f"[check-phone] ping_sender query failed (non-critical): {e}")

    return CheckPhoneResponse(
        status="override",
        contact_id=contact["id"],
        contact_name=display_name,
        contact_number=contact["number"],
        ping_step=ping_step,
        ping_sender_id=ping_sender_id,
    )


# ══════════════════════════════════════════════════════════════════════
# CRUD Operations
# ══════════════════════════════════════════════════════════════════════

@router.get("/calls/{call_id}/messages")
async def get_call_messages(
    call_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        messages = (
            db.table("messages")
            .select("*")
            .eq("call_id", call_id)
            .order("sent_at", desc=False)
            .execute()
        )
        return {"messages": messages.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/contacts/{contact_id}/messages")
async def get_contact_messages(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        messages = (
            db.table("messages")
            .select("*")
            .eq("contact_id", contact_id)
            .order("sent_at", desc=False)
            .execute()
        )
        return {"messages": messages.data or []}
    except Exception as e:
        logger.error(f"Error getting messages for contact {contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/contacts")
async def list_contacts(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
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
    logger.info(f"Creating contact for phone {phone_id}", extra={
        "action": "create_contact",
        "phone_id": phone_id
    })
    try:
        contact_data = {
            "phone_id": phone_id,
            "number":   body.get("phone", "").replace("+", ""),
            "name":     body.get("name"),
            "email":    body.get("email"),
            "tag":      body.get("tag", "new"),   # ← היה "חדש"
            "lid":      body.get("lid"),
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
    logger.info(f"Updating contact {contact_id}", extra={
        "action": "update_contact",
        "contact_id": contact_id
    })
    try:
        update_data = {}
        if body.name  is not None: update_data["name"]  = body.name
        if body.email is not None: update_data["email"] = body.email
        if body.tag   is not None: update_data["tag"]   = body.tag
        if body.lid   is not None: update_data["lid"]   = body.lid

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
    logger.info(f"[PING] Creating contact from ping: {body.target_number}", extra={
        "action": "ping_create",
        "phone_id": body.phone_id,
        "target": body.target_number
    })

    clean_number = "".join(filter(str.isdigit, body.target_number))

    if len(clean_number) < 7 or len(clean_number) > 15:
        raise HTTPException(status_code=400, detail="Invalid phone number length (7-15 digits)")

    try:
        # ── OVERRIDE: השתמש ב-contact קיים, אפס LID ──────────────────
        if body.override_contact_id:
            existing = (
                db.table("contacts")
                .select("*")
                .eq("id", body.override_contact_id)
                .execute()
            )
            if not existing.data:
                raise HTTPException(status_code=404, detail="Contact not found for override")
            contact = existing.data[0]
            db.table("contacts").update({
                "lid":  None,
                "tag":  "draft",
                "name": body.name or contact.get("name"),
            }).eq("id", body.override_contact_id).execute()
            contact = {**contact, "lid": None, "tag": "draft"}
            logger.info(f"[PING] Override contact: {contact['id']}")

        # ── רגיל: חפש לפי מספר או צור חדש ──────────────────────────
        else:
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
                contact_data = {
                    "phone_id": body.phone_id,
                    "number":   clean_number,
                    "name":     body.name or clean_number,
                    "lid":      None,
                    "tag":      "new",   # contact מקורי — גלוי בטבלה
                }
                result = db.table("contacts").insert(contact_data).execute()
                contact = result.data[0]
                logger.info(f"[PING] Created new contact: {contact['id']}")

        # ── שלח PING לאגנט ────────────────────────────────────────────
        agent_info = await _get_agent_ip_for_phone(db, body.phone_id)
        if not agent_info:
            raise HTTPException(status_code=404, detail="Agent host not found for this phone.")

        agent_ip, host_id = agent_info
        if not _is_valid_ip(agent_ip):
            raise HTTPException(status_code=400, detail=f"Invalid agent IP: {agent_ip}")

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

        ping_sender_id = ping_result.get("pingSenderId")
        logger.info(f"[PING] Success! pingSenderId: {ping_sender_id}")

        # ── עדכן ping_sender עם contact_id (נוצר ע"י האגנט ללא contact_id) ──
        if ping_sender_id:
            try:
                db.table("ping_sender").update({
                    "contact_id": contact["id"],
                    "status":     "pending",
                }).eq("id", ping_sender_id).execute()
                logger.info(f"[PING] ping_sender {ping_sender_id} linked to contact {contact['id']}")
            except Exception as e:
                logger.warning(f"[PING] Failed to update ping_sender contact_id: {e}")

        return {
            "success":             True,
            "contact_id":          contact["id"],
            "ping_sender_id":      ping_sender_id,
            "whatsapp_message_id": ping_result.get("messageId"),
            "message":             "PING sent successfully. Waiting for response...",
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"[PING] Agent HTTP error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Agent returned error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"[PING] Agent unreachable: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot reach agent at {agent_ip}.")
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

        contacts_res = (
            db.table("contacts")
            .select("id, number, name, lid, tag, whatsapp_name")
            .eq("phone_id", phone_id)
            .in_("tag", ["draft"])
            .is_("lid", "null")      # רק draft שטרם קושרו — יש להם ping_sender פעיל
            .execute()
        )

        contact_map = {}

        for contact in contacts_res.data or []:
            contact_id = contact["id"]

            messages = (
                db.table("messages")
                .select("*")
                .eq("contact_id", contact_id)
                .eq("direction", True)
                .gt("sent_at", cutoff)
                .order("sent_at", desc=False)
                .execute()
            )

            msgs = messages.data or []
            if not msgs:
                continue

            display_name = (
                contact.get("whatsapp_name") or
                contact.get("name") or
                contact.get("number")
            )

            contact_map[contact_id] = {
                "contact": {
                    "id":     contact["id"],
                    "name":   display_name,
                    "number": contact["number"],
                    "lid":    contact.get("lid"),
                    "tag":    contact.get("tag"),
                },
                "messages":     msgs,
                "last_message": msgs[-1],
            }

        return {"conversations": list(contact_map.values())}

    except Exception as e:
        logger.error(f"[PING] Error fetching conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 3: Select response and link LID
# ══════════════════════════════════════════════════════════════════════

@router.post("/contacts/select-response")
async def select_response(
    body: SelectResponseRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        message = (
            db.table("messages")
            .select("sender, leaf_id, call_id, content")
            .eq("id", body.message_id)
            .single()
            .execute()
        )

        if not message.data:
            raise HTTPException(status_code=404, detail="Message not found")

        selected_lid    = message.data.get("sender")
        message_content = message.data.get("content", {})

        whatsapp_name = None
        if isinstance(message_content, dict):
            whatsapp_name = (
                message_content.get("pushName") or
                message_content.get("notifyName") or
                message_content.get("verifiedBizName")
            )

        if not selected_lid:
            raise HTTPException(status_code=400, detail="Selected message has no LID")

        update_data = {
            "lid":        selected_lid,
            "tag":        "active",   # ← היה "לקוח"
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

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

        try:
            # ניסיון 1 — חפש לפי contact_id
            ps_res = db.table("ping_sender").update({
                "status":             "completed",
                "matched_contact_id": body.contact_id,
                "contact_id":         body.contact_id,
            }).eq("contact_id", body.contact_id).eq("status", "pending").execute()

            # ניסיון 2 — אם contact_id היה null, חפש לפי phone_id + status
            if not ps_res.data:
                db.table("ping_sender").update({
                    "status":             "completed",
                    "contact_id":         body.contact_id,
                    "matched_contact_id": body.contact_id,
                }).eq("phone_id", result.data[0].get("phone_id")).eq("status", "pending").is_("contact_id", "null").execute()
                logger.info(f"[PING] ping_sender updated via phone_id fallback")
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
