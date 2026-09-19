# routers/contacts.py
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timedelta, timezone

from dependencies import get_supabase, get_current_user
from routers.contact_lang import (               # ⬅️ חדש
    get_user_lang_by_phone, get_contact_lang,
    get_ping_message, normalize_lang,
)
from supabase import Client
from logging_config import get_logger

logger = get_logger("contacts")

router = APIRouter(tags=["contacts"])

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "secret-token-123")


# ══════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════

class CreateContactFromPingRequest(BaseModel):
    phone_id: str = Field(..., description="Phone that sends the PING.")
    target_number: str = Field(..., description="Number to PING. Non-digits are removed; 7-15 digits.")
    name: Optional[str] = Field(None, description="Contact name. New contacts default to the number.")
    override_contact_id: Optional[str] = Field(None, description="Existing contact to reset and PING again, instead of matching by number.")
    lang: Optional[str] = Field(None, description="PING language. If omitted: the existing contact's language, then the phone owner's language.")  # ⬅️ אם לא נשלח — נגזר בשרת


class SelectResponseRequest(BaseModel):
    contact_id: str = Field(..., description="Draft contact that sent the selected reply.")  # draft contact שנבחר (עם LID)
    message_id: str = Field(..., description="Reply message whose sender LID is linked.")
    parent_contact_id: Optional[str] = Field(None, description="Contact to activate. Defaults to contact_id.")  # new contact לעדכון


class UpdateContactRequest(BaseModel):
    name: Optional[str] = Field(None, description="Contact name.")
    email: Optional[str] = Field(None, description="Contact email.")
    tag: Optional[str] = Field(None, description="Contact tag, for example new, draft or active.")
    lid: Optional[str] = Field(None, description="WhatsApp LID of the contact.")
    lang: Optional[str] = Field(None, description="Contact language; normalized on save.")  # ⬅️ ניתן לעריכה


class CheckPhoneResponse(BaseModel):
    status: str = Field(..., description="new, blocked or override.")
    contact_id: Optional[str] = Field(None, description="Existing contact id.")
    contact_name: Optional[str] = Field(None, description="WhatsApp name, name or number of the contact.")
    contact_number: Optional[str] = Field(None, description="Number stored on the contact.")
    ping_step: Optional[str] = Field(None, description="Status of the latest open PING: pending or waiting_reply.")
    ping_sender_id: Optional[str] = Field(None, description="Id of that PING record.")


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

BOGUS_LID_VALUES = {"status", "broadcast", "0", "", "null", "undefined"}

def _is_valid_lid(lid: Optional[str]) -> bool:
    return bool(lid and lid.strip() and lid.strip().lower() not in BOGUS_LID_VALUES)


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
        host  = phone.get("agent_hosts")
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


async def _get_user_id_for_phone(db: Client, phone_id: str) -> Optional[str]:
    """שלוף user_id מה-phone — לאכלס על contacts חדשים"""
    try:
        res = db.table("phones").select("user_id").eq("id", phone_id).limit(1).execute()
        return res.data[0]["user_id"] if res.data else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# check-phone
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts/check-phone", response_model=CheckPhoneResponse, summary="Check number before PING", description="Looks up a number on the phone. Returns 'new' when there is no contact, 'blocked' when the contact already has a real LID, or 'override' together with any pending PING step.")
async def check_phone(
    phone_id: str = Query(..., description="Phone to search in."),
    number: str = Query(..., description="Number to check. Non-digits are removed."),
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
        logger.error(f"[check-phone] error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not contact_res.data:
        return CheckPhoneResponse(status="new")

    contact = contact_res.data[0]
    display_name = (
        contact.get("whatsapp_name") or
        contact.get("name") or
        contact.get("number")
    )

    contact_lid          = contact.get("lid")
    contact_number_clean = "".join(filter(str.isdigit, contact.get("number") or ""))
    lid_is_real          = _is_valid_lid(contact_lid) and contact_lid != contact_number_clean

    if lid_is_real:
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
            ping_step      = ping_res.data[0]["status"]
            ping_sender_id = ping_res.data[0]["id"]
    except Exception as e:
        logger.warning(f"[check-phone] ping_sender query failed: {e}")

    return CheckPhoneResponse(
        status="override",
        contact_id=contact["id"],
        contact_name=display_name,
        contact_number=contact["number"],
        ping_step=ping_step,
        ping_sender_id=ping_sender_id,
    )


# ══════════════════════════════════════════════════════════════════════
# CRUD
# ══════════════════════════════════════════════════════════════════════

@router.get("/calls/{call_id}/messages", summary="List call messages", description="Returns every message linked to the call, oldest first.")
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


@router.get("/contacts/{contact_id}/messages", summary="List contact messages (raw)", description="Returns every message of the contact, oldest first, as stored rows.")
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


@router.get("/contacts", summary="List contacts", description="Returns the phone's contacts, most recently updated first.")
async def list_contacts(
    phone_id: str = Query(..., description="Phone whose contacts to list."),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        result = (
            db.table("contacts")
            .select(
                "id, phone_id, number, name, whatsapp_name, tag, lid, lang, "  # ⬅️ lang
                "is_connect, parent_contact_id, created_at, updated_at"
            )
            .eq("phone_id", phone_id)
            .order("updated_at", desc=True)
            .execute()
        )
        return {"contacts": result.data or []}
    except Exception as e:
        logger.error(f"Error listing contacts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/contacts", summary="Create contact", description="Creates a contact on the phone. Body: phone, name, email, tag (default 'new'), lid, lang. Without lang the phone owner's language is used.")
async def create_contact(
    body: dict,
    phone_id: str = Query(..., description="Phone to create the contact on."),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        clean_num = body.get("phone", "").replace("+", "").replace(" ", "")
        user_id   = await _get_user_id_for_phone(db, phone_id)

        # ⬅️ שפה: מפורש ← שפת המשתמש
        lang = (normalize_lang(body.get("lang")) if body.get("lang")
                else get_user_lang_by_phone(db, phone_id))

        contact_data = {
            "phone_id": phone_id,
            "number":   clean_num,
            "name":     body.get("name"),
            "email":    body.get("email"),
            "tag":      body.get("tag", "new"),
            "lid":      body.get("lid") or clean_num,
            "user_id":  user_id,
            "lang":     lang,                        # ⬅️
        }
        result = db.table("contacts").insert(contact_data).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        logger.error(f"Error creating contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/contacts/{contact_id}", summary="Get contact", description="Returns a fresh copy of one contact from the database.")
async def get_contact(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """שליפת שורה טרייה מה-DB — נקרא ב-EditContactModal בזמן פתיחה,
    כדי לא להסתמך על ה-state המקומי (עלול להיות מיושן)."""
    try:
        result = (
            db.table("contacts")
            .select(
                "id, phone_id, number, name, whatsapp_name, tag, lid, lang, "
                "email, is_connect, parent_contact_id, created_at, updated_at"
            )
            .eq("id", contact_id)
            .single()
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Contact not found")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting contact {contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/contacts/{contact_id}", summary="Update contact", description="Updates name, email, tag, lid or lang. Only the fields that are sent change.")
async def update_contact(
    contact_id: str,
    body: UpdateContactRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        update_data = {}
        if body.name  is not None: update_data["name"]  = body.name
        if body.email is not None: update_data["email"] = body.email
        if body.tag   is not None: update_data["tag"]   = body.tag
        if body.lid   is not None: update_data["lid"]   = body.lid
        if body.lang  is not None: update_data["lang"]  = normalize_lang(body.lang)  # ⬅️

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


@router.delete("/contacts/{contact_id}", summary="Delete contact", description="Deletes the contact together with its messages and PING records, and unlinks drafts that pointed to it. Returns the number of deleted messages.")
async def delete_contact(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        deleted_msgs = db.table("messages").delete().eq("contact_id", contact_id).execute()
        msg_count = len(deleted_msgs.data or [])

        db.table("ping_sender").delete().eq("contact_id", contact_id).execute()

        db.table("contacts").update({
            "parent_contact_id": None,
            "is_connect":        False,
        }).eq("parent_contact_id", contact_id).execute()

        db.table("contacts").delete().eq("id", contact_id).execute()

        return {"ok": True, "deleted_messages": msg_count}
    except Exception as e:
        logger.error(f"Error deleting contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 1: שלח PING
# ══════════════════════════════════════════════════════════════════════

@router.post("/contacts/create-from-ping", summary="Send PING (step 1)", description="Creates, reuses or overrides the contact for a number, then sends a PING message in the contact's language through the agent. Unlinked drafts with a real LID are linked to this contact.")
async def create_contact_from_ping(
    body: CreateContactFromPingRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    clean_number = "".join(filter(str.isdigit, body.target_number))
    if len(clean_number) < 7 or len(clean_number) > 15:
        raise HTTPException(status_code=400, detail="Invalid phone number length (7-15 digits)")

    try:
        user_id = await _get_user_id_for_phone(db, body.phone_id)

        # ⬅️ שפה: מפורש ← איש קשר קיים (override) ← שפת המשתמש
        if body.lang:
            lang = normalize_lang(body.lang)
        elif body.override_contact_id:
            lang = get_contact_lang(db, body.override_contact_id)
        else:
            lang = get_user_lang_by_phone(db, body.phone_id)

        if body.override_contact_id:
            existing = db.table("contacts").select("*").eq("id", body.override_contact_id).execute()
            if not existing.data:
                raise HTTPException(status_code=404, detail="Contact not found for override")
            contact = existing.data[0]

            upd = {
                "lid":  None,
                "tag":  "new",
                "name": body.name or contact.get("name"),
            }
            if body.lang:                               # ⬅️ רק אם נבחרה שפה מפורשת
                upd["lang"] = lang
            db.table("contacts").update(upd).eq("id", body.override_contact_id).execute()
            contact = {**contact, "lid": None, "tag": "new"}

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

                # ⬅️ איש קשר קיים לפי מספר — עדיפות לשפה השמורה עליו
                if not body.lang and contact.get("lang"):
                    lang = normalize_lang(contact["lang"])

                if contact.get("tag") == "draft":
                    db.table("contacts").update({
                        "tag":  "new",
                        "name": body.name or contact.get("name") or clean_number,
                    }).eq("id", contact["id"]).execute()
                    contact = {**contact, "tag": "new"}
                    logger.info(f"[PING] Upgraded draft → new: {contact['id']}")
                else:
                    logger.info(f"[PING] Contact already exists: {contact['id']}")
            else:
                try:
                    result = db.table("contacts").insert({
                        "phone_id": body.phone_id,
                        "number":   clean_number,
                        "name":     body.name or clean_number,
                        "lid":      clean_number,
                        "tag":      "new",
                        "user_id":  user_id,
                        "lang":     lang,               # ⬅️
                    }).execute()
                    contact = result.data[0]
                    logger.info(f"[PING] Created new contact: {contact['id']} lang={lang}")
                except Exception as insert_err:
                    if "23505" in str(insert_err) or "duplicate key" in str(insert_err):
                        existing2 = (
                            db.table("contacts")
                            .select("*")
                            .eq("phone_id", body.phone_id)
                            .eq("lid", clean_number)
                            .limit(1)
                            .execute()
                        )
                        if existing2.data:
                            contact = existing2.data[0]
                            if not body.lang and contact.get("lang"):     # ⬅️
                                lang = normalize_lang(contact["lang"])
                            logger.info(f"[PING] Found existing by lid fallback: {contact['id']}")
                        else:
                            raise
                    else:
                        raise

        agent_info = await _get_agent_ip_for_phone(db, body.phone_id)
        if not agent_info:
            raise HTTPException(status_code=404, detail="Agent host not found")

        agent_ip, _ = agent_info
        if not _is_valid_ip(agent_ip):
            raise HTTPException(status_code=400, detail=f"Invalid agent IP: {agent_ip}")

        # ── תבנית ל-PING: check_contact → hello_world → כל תבנית מאושרת ──
        from routers.template_manager import pick_ping_template
        tpl = pick_ping_template(db, body.phone_id)

        jid       = f"{clean_number}@s.whatsapp.net"
        base_url  = f"http://{agent_ip}:5000/api/phones/{body.phone_id}/send"
        ping_text = get_ping_message(lang)

        async def _send(url: str, payload: dict) -> dict:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"X-Agent-Token": AGENT_TOKEN, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                return response.json()

        ping_result = None

        # התבנית היא ניסיון ראשון בלבד — דחייה של ה-HostAgent לא מפילה את ה-PING
        if tpl:
            pm = tpl.get("params") or {}
            ex = tpl.get("examples") or {}
            send_body = {
                "jid":        jid,
                "name":       tpl["name"],
                "lang":       tpl["lang"],
                "templateId": tpl["id"],
                "params": {
                    "header": list(ex.get("header") or [])[:len(pm.get("header") or [])],
                    "body":   list(ex.get("body") or [])[:len(pm.get("body") or [])],
                },
            }
            try:
                ping_result = await _send(f"{base_url}/ping-template", send_body)
                logger.info(f"[PING] template={tpl['name']}/{tpl['lang']} contact={contact['id']}")
            except httpx.HTTPStatusError as e:
                logger.warning(
                    f"[PING] template {tpl['name']}/{tpl['lang']} rejected "
                    f"({e.response.status_code}): {e.response.text[:300]} — נופל לטקסט"
                )
                tpl = None

        if ping_result is None:
            ping_result = await _send(f"{base_url}/ping", {"jid": jid, "text": ping_text})
            logger.info(f"[PING] plain text lang={lang} contact={contact['id']}")

        ping_sender_id      = ping_result.get("pingSenderId")
        whatsapp_message_id = ping_result.get("messageId")
        logger.info(f"[PING] pingSenderId={ping_sender_id} messageId={whatsapp_message_id}")

        if ping_sender_id:
            try:
                db.table("ping_sender").update({
                    "contact_id": contact["id"],
                }).eq("id", ping_sender_id).execute()
                logger.info(f"[PING] ping_sender {ping_sender_id} → contact {contact['id']}")
            except Exception as e:
                logger.warning(f"[PING] Failed to update contact_id: {e}")

        try:
            draft_res = (
                db.table("contacts")
                .select("id, lid, number")
                .eq("phone_id", body.phone_id)
                .eq("tag", "draft")
                .is_("parent_contact_id", "null")
                .neq("id", contact["id"])
                .execute()
            )
            for draft in draft_res.data or []:
                draft_lid    = draft.get("lid") or ""
                draft_number = draft.get("number") or ""
                if _is_valid_lid(draft_lid) and draft_lid != draft_number:
                    db.table("contacts").update({
                        "parent_contact_id": contact["id"]
                    }).eq("id", draft["id"]).execute()
                    logger.info(f"[PING] Linked draft {draft['id']} → parent {contact['id']}")
        except Exception as e:
            logger.warning(f"[PING] Failed to link drafts: {e}")

        return {
            "success":             True,
            "contact_id":          contact["id"],
            "ping_sender_id":      ping_sender_id,
            "whatsapp_message_id": whatsapp_message_id,
            "lang":                lang,          # ⬅️ ה-UI יכול להציג מה נשלח
            "ping_text":           ping_text,     # ⬅️
            "message":             "PING sent successfully. Waiting for response...",
        }

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail=f"Cannot reach agent at {agent_ip}.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PING] Unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 2: שלוף שיחות ממתינות
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts/outgoing-with-replies/{phone_id}", summary="List PING replies (step 2)", description="Returns draft contacts with a valid LID that have messages from the last 24 hours, with those messages, so the user can pick the reply that answers the PING.")
async def get_outgoing_with_replies(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        contact_map = {}

        ps_res = (
            db.table("ping_sender")
            .select("id, contact_id, target_number, status")
            .eq("phone_id", phone_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        active_ps       = ps_res.data[0] if ps_res.data else None
        main_contact_id = active_ps.get("contact_id") if active_ps else None

        drafts_res = (
            db.table("contacts")
            .select("id, number, name, lid, tag, whatsapp_name, parent_contact_id, is_connect")
            .eq("phone_id", phone_id)
            .eq("tag", "draft")
            .execute()
        )

        valid_drafts = [
            d for d in (drafts_res.data or [])
            if _is_valid_lid(d.get("lid"))
        ]

        for draft in valid_drafts:
            draft_id = draft["id"]

            msgs_res = (
                db.table("messages")
                .select("*")
                .eq("contact_id", draft_id)
                .eq("direction", True)
                .gt("sent_at", cutoff)
                .order("sent_at", desc=False)
                .execute()
            )
            msgs = msgs_res.data or []
            if not msgs:
                continue

            display_name = (
                draft.get("whatsapp_name") or
                draft.get("name") or
                draft.get("lid") or
                draft.get("number")
            )

            parent = draft.get("parent_contact_id") or main_contact_id

            contact_map[draft_id] = {
                "contact": {
                    "id":                draft_id,
                    "name":              display_name,
                    "number":            draft["number"],
                    "lid":               draft.get("lid"),
                    "tag":               draft.get("tag"),
                    "parent_contact_id": parent,
                },
                "messages":     msgs,
                "last_message": msgs[-1],
            }

        return {"conversations": list(contact_map.values())}

    except Exception as e:
        logger.error(f"[PING] Error fetching conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# PING Flow - Step 3: בחירת תגובה וקישור LID
# ══════════════════════════════════════════════════════════════════════

@router.post("/contacts/select-response", summary="Select PING reply (step 3)", description="Takes the LID from the chosen message, sets it on the target contact and tags it 'active'. Clears the LID from the draft, links remaining drafts to the contact and completes the pending PING.")
async def select_response(
    body: SelectResponseRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        message = (
            db.table("messages")
            .select("sender, content")
            .eq("id", body.message_id)
            .single()
            .execute()
        )
        if not message.data:
            raise HTTPException(status_code=404, detail="Message not found")

        selected_lid    = message.data.get("sender")
        message_content = message.data.get("content", {})

        if not selected_lid or not _is_valid_lid(selected_lid):
            raise HTTPException(status_code=400, detail=f"Invalid LID: {selected_lid}")

        whatsapp_name = None
        if isinstance(message_content, dict):
            whatsapp_name = (
                message_content.get("pushName") or
                message_content.get("notifyName") or
                message_content.get("verifiedBizName")
            )

        target_contact_id = body.parent_contact_id or body.contact_id
        draft_contact_id  = body.contact_id

        # ── נקה LID מה-draft ────────────────────────────────────────
        if draft_contact_id != target_contact_id:
            try:
                draft_res = (
                    db.table("contacts")
                    .select("whatsapp_name")
                    .eq("id", draft_contact_id)
                    .limit(1)
                    .execute()
                )
                draft_whatsapp_name = draft_res.data[0].get("whatsapp_name") if draft_res.data else None

                db.table("contacts").update({
                    "lid":        None,
                    "is_connect": True,
                }).eq("id", draft_contact_id).execute()
                logger.info(f"[PONG] Cleared LID from draft {draft_contact_id}")

                if draft_whatsapp_name and not whatsapp_name:
                    whatsapp_name = draft_whatsapp_name
            except Exception as e:
                logger.warning(f"[PONG] Failed to clear draft LID: {e}")

        # ── עדכן ה-new contact → active ────────────────────────────
        update_data = {
            "lid":        selected_lid,
            "tag":        "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if whatsapp_name:
            update_data["whatsapp_name"] = whatsapp_name

        result = (
            db.table("contacts")
            .update(update_data)
            .eq("id", target_contact_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Contact not found")

        logger.info(f"[PONG] Contact {target_contact_id} → active | LID={selected_lid}")

        # ── ✅ עדכן parent_contact_id על ה-draft ────────────────────
        if draft_contact_id != target_contact_id:
            try:
                db.table("contacts").update({
                    "parent_contact_id": target_contact_id
                }).eq("id", draft_contact_id).execute()
                logger.info(f"[PONG] Draft {draft_contact_id} → parent {target_contact_id}")
            except Exception as e:
                logger.warning(f"[PONG] Failed to set parent_contact_id on draft: {e}")

        # ── עדכן כל שאר ה-drafts שקשורים לפי LID ──────────────────
        try:
            db.table("contacts").update({
                "parent_contact_id": target_contact_id
            }).eq("phone_id", result.data[0].get("phone_id", "")) \
              .eq("tag", "draft") \
              .is_("parent_contact_id", "null") \
              .execute()
            logger.info(f"[PONG] Linked all remaining drafts → parent {target_contact_id}")
        except Exception as e:
            logger.warning(f"[PONG] Failed to link remaining drafts: {e}")

        # ── עדכן ping_sender → completed ───────────────────────────
        try:
            ps_res = (
                db.table("ping_sender")
                .update({"status": "completed"})
                .eq("contact_id", target_contact_id)
                .eq("status", "pending")
                .execute()
            )
            if not ps_res.data:
                phone_id = result.data[0].get("phone_id")
                db.table("ping_sender").update({
                    "status": "completed"
                }).eq("phone_id", phone_id).eq("status", "pending").execute()
                logger.info(f"[PONG] ping_sender completed via phone_id fallback")
        except Exception as e:
            logger.warning(f"[PONG] Failed to update ping_sender: {e}")

        return {
            "success": True,
            "message": "Contact linked successfully!",
            "contact": result.data[0],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PONG] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════
# Agent Webhook — קישור draft לparent
# ══════════════════════════════════════════════════════════════════════

class LinkDraftRequest(BaseModel):
    phone_id: str = Field(..., description="Phone that received the message.")
    draft_contact_id: str = Field(..., description="Draft contact to link.")
    lid: str = Field(..., description="LID seen on the incoming message. Bogus values are ignored.")


@router.post("/contacts/link-draft-to-parent", summary="Link draft to PING contact", description="Called by the agent webhook. Links a draft contact to the contact of the latest pending PING on the phone.")
async def link_draft_to_parent(
    body: LinkDraftRequest,
    db: Client = Depends(get_supabase),
):
    if not _is_valid_lid(body.lid):
        return {"ok": False, "reason": "bogus lid"}

    try:
        ps_res = (
            db.table("ping_sender")
            .select("id, contact_id")
            .eq("phone_id", body.phone_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not ps_res.data:
            return {"ok": False, "reason": "no active ping_sender"}

        parent_contact_id = ps_res.data[0].get("contact_id")
        if not parent_contact_id:
            return {"ok": False, "reason": "ping_sender has no contact_id"}

        db.table("contacts").update({
            "parent_contact_id": parent_contact_id
        }).eq("id", body.draft_contact_id).execute()

        return {"ok": True, "parent_contact_id": parent_contact_id}

    except Exception as e:
        logger.error(f"[link-draft] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
