from fastapi import APIRouter, Depends, HTTPException, Query
from dependencies import get_supabase, get_current_user
from supabase import Client
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import httpx, os, uuid

router = APIRouter(prefix="/calls", tags=["calls"])

BACKEND_URL = os.getenv("BACKEND_URL", "https://vid.michal-solutions.com/api")


# ── Schemas ───────────────────────────────────────────────────────────

class StartCallRequest(BaseModel):
    phone_id:    str
    contact_id:  str
    scenario_id: str | None = None
    duration_seconds: int = 300          # ברירת מחדל: 5 דקות


class EndCallRequest(BaseModel):
    call_id: str
    status:  str = "completed"           # completed | failed | cancelled


# ── Helpers ───────────────────────────────────────────────────────────

async def _get_agent_info(db: Client, phone_id: str):
    """מחזיר ip + api_port של ה-agent עבור phone_id"""
    res = (
        db.table("phones")
        .select("number, api_port, agent_hosts(ip_address)")
        .eq("id", phone_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None, None, ""
    row      = res.data[0]
    host     = row.get("agent_hosts") or {}
    ip       = host.get("ip_address")
    api_port = row.get("api_port")
    number   = row.get("number", "")
    return ip, api_port, number


async def _get_contact_number(db: Client, contact_id: str) -> str:
    res = (
        db.table("contacts")
        .select("phone_number")
        .eq("id", contact_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return ""
    return res.data[0].get("phone_number", "")


# ── Endpoints ─────────────────────────────────────────────────────────

@router.post("/start")
async def start_call(
    body: StartCallRequest,
    db:   Client = Depends(get_supabase),
    user         = Depends(get_current_user),
):
    """
    1. יוצר רשומת call ב-DB עם started_at + expected_end
    2. שולח הודעת WhatsApp ראשונה דרך ה-agent
    3. מחזיר call_id + expected_end לצד הלקוח
    """

    # ── א. שלוף מידע על ה-agent ────────────────────────────────────
    ip, api_port, phone_number = await _get_agent_info(db, body.phone_id)
    if not ip or not api_port:
        raise HTTPException(404, "Agent not found for this phone")

    contact_number = await _get_contact_number(db, body.contact_id)
    if not contact_number:
        raise HTTPException(404, "Contact phone number not found")

    # ── ב. צור רשומת call ─────────────────────────────────────────
    now        = datetime.now(timezone.utc)
    expected   = now + timedelta(seconds=body.duration_seconds)
    call_id    = str(uuid.uuid4())

    call_row = {
        "id":           call_id,
        "phone_id":     body.phone_id,
        "contact_id":   body.contact_id,
        "scenario_id":  body.scenario_id,
        "status":       "active",
        "started_at":   now.isoformat(),
        "expected_end": expected.isoformat(),
        "created_at":   now.isoformat(),
        "last_status_updated_at": now.isoformat(),
    }

    ins = db.table("calls").insert(call_row).execute()
    if not ins.data:
        raise HTTPException(500, "Failed to create call record")

    # ── ג. שלח הודעה ראשונה דרך ה-agent ──────────────────────────
    agent_url = f"http://{ip}:{api_port}/send"
    payload   = {
        "to":      contact_number,
        "message": "שיחה התחילה",          # ניתן להתאים / לקחת מה-scenario
        "call_id": call_id,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(agent_url, json=payload)
            agent_ok = r.status_code < 300
    except Exception:
        agent_ok = False

    return {
        "call_id":      call_id,
        "status":       "active",
        "started_at":   now.isoformat(),
        "expected_end": expected.isoformat(),
        "agent_sent":   agent_ok,
        "phone_id":     body.phone_id,
        "contact_id":   body.contact_id,
    }


# ── Polling ───────────────────────────────────────────────────────────

@router.get("/{call_id}/messages")
async def poll_call_messages(
    call_id:    str,
    since:      str | None = Query(None, description="ISO timestamp — מחזיר רק הודעות אחריו"),
    db:         Client     = Depends(get_supabase),
    user                   = Depends(get_current_user),
):
    """
    מחזיר הודעות חדשות עבור ה-call:
    - שולף phone_id + contact_id + started_at מה-call
    - מחזיר הודעות שנשלחו מ-started_at (ואם `since` ניתן — מאז since)
    - מחזיר גם את סטטוס ה-call (active / completed / failed)
    """
    call_res = (
        db.table("calls")
        .select("id, phone_id, contact_id, started_at, expected_end, status, ended_at")
        .eq("id", call_id)
        .limit(1)
        .execute()
    )
    if not call_res.data:
        raise HTTPException(404, "Call not found")

    call = call_res.data[0]

    # קבע from_ts
    from_ts = since or call.get("started_at") or ""

    query = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .eq("phone_id",   call["phone_id"])
        .eq("contact_id", call["contact_id"])
        .order("sent_at")
    )
    if from_ts:
        query = query.gte("sent_at", from_ts)

    msgs_res = query.limit(200).execute()

    # ── סטטוס אוטומטי: אם עבר expected_end ──────────────────────
    call_status = call.get("status", "active")
    expected    = call.get("expected_end")
    if call_status == "active" and expected:
        try:
            exp_dt = datetime.fromisoformat(expected.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                call_status = "completed"
                db.table("calls").update({
                    "status":   "completed",
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "last_status_updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", call_id).execute()
        except Exception:
            pass

    # שלוף phone_number לצורך format_message
    phone_res    = db.table("phones").select("number").eq("id", call["phone_id"]).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    # reuse format_message מ-messages router (import local)
    from routers.messages import format_message
    messages = [format_message(m, phone_number, call["phone_id"]) for m in (msgs_res.data or [])]

    return {
        "call_id":      call_id,
        "call_status":  call_status,
        "expected_end": expected,
        "ended_at":     call.get("ended_at"),
        "messages":     messages,
    }


# ── End call manually ─────────────────────────────────────────────────

@router.post("/end")
async def end_call(
    body: EndCallRequest,
    db:   Client = Depends(get_supabase),
    user         = Depends(get_current_user),
):
    now = datetime.now(timezone.utc).isoformat()
    res = (
        db.table("calls")
        .update({
            "status":   body.status,
            "ended_at": now,
            "last_status_updated_at": now,
        })
        .eq("id", body.call_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Call not found")
    return {"call_id": body.call_id, "status": body.status, "ended_at": now}


# ── Get single call ───────────────────────────────────────────────────

@router.get("/{call_id}")
async def get_call(
    call_id: str,
    db:      Client = Depends(get_supabase),
    user            = Depends(get_current_user),
):
    res = (
        db.table("calls")
        .select("*")
        .eq("id", call_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Call not found")
    return res.data[0]