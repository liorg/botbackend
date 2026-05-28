from fastapi import APIRouter, Depends, HTTPException, Query
from dependencies import get_supabase, get_current_user
from supabase import Client
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import httpx, os, uuid

router = APIRouter(prefix="/calls", tags=["calls"])

BACKEND_URL = os.getenv("BACKEND_URL", "https://vid.michal-solutions.com/api")


class StartCallRequest(BaseModel):
    phone_id:         str
    contact_id:       str
    scenario_id:      str | None = None
    duration_seconds: int = 300


class EndCallRequest(BaseModel):
    call_id: str
    status:  str = "completed"


async def _get_agent_info(db: Client, phone_id: str):
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
        .select("number")
        .eq("id", contact_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return ""
    return res.data[0].get("number", "")


@router.post("/start")
async def start_call(
    body: StartCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    ip, api_port, phone_number = await _get_agent_info(db, body.phone_id)
    if not ip or not api_port:
        raise HTTPException(404, "Agent not found for this phone")

    contact_number = await _get_contact_number(db, body.contact_id)
    if not contact_number:
        raise HTTPException(404, "Contact phone number not found")

    now      = datetime.now(timezone.utc)
    expected = now + timedelta(seconds=body.duration_seconds)
    call_id  = str(uuid.uuid4())

    ins = db.table("calls").insert({
        "id":                     call_id,
        "phone_id":               body.phone_id,
        "contact_id":             body.contact_id,
        "scenario_id":            body.scenario_id,
        "status":                 "active",
        "started_at":             now.isoformat(),
        "expected_end":           expected.isoformat(),
        "created_at":             now.isoformat(),
        "last_status_updated_at": now.isoformat(),
    }).execute()

    if not ins.data:
        raise HTTPException(500, "Failed to create call record")

    # ── רשום webhook לקבלת הודעות נכנסות ─────────────────────────────────
    callback_url = f"{BACKEND_URL}/webhook-registrations/callback/{body.phone_id}/{body.contact_id}"
    db.table("webhook_registrations") \
      .delete() \
      .eq("phone_id",   body.phone_id) \
      .eq("contact_id", body.contact_id) \
      .execute()
    db.table("webhook_registrations").insert({
        "id":           str(uuid.uuid4()),
        "phone_id":     body.phone_id,
        "contact_id":   body.contact_id,
        "callback_url": callback_url,
        "type":         "recording",
        "is_active":    True,
        "created_at":   now.isoformat(),
    }).execute()

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"http://{ip}:{api_port}/send",
                json={"to": contact_number, "message": "שיחה התחילה", "call_id": call_id},
            )
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


@router.get("/{call_id}/messages")
async def poll_call_messages(
    call_id: str,
    since:   str | None = Query(None),
    limit:   int        = Query(50, le=200),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
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

    # ── אסוף contact_ids: call + scenario + כל הילדים ──────────────────────
    contact_ids = set()
    contact_ids.add(call["contact_id"])

    if call.get("scenario_id"):
        sc_res = db.table("scenarios").select("contact_id").eq("id", call["scenario_id"]).limit(1).execute()
        sc_contact = (sc_res.data or [{}])[0].get("contact_id")
        if sc_contact:
            contact_ids.add(sc_contact)

    for cid in list(contact_ids):
        child_res = db.table("contacts").select("id").eq("parent_contact_id", cid).execute()
        for c in (child_res.data or []):
            contact_ids.add(c["id"])

    all_ids = list(contact_ids)

    # השתמש ב-since אם נשלח, אחרת started_at
    from_ts = since or call.get("started_at") or ""

    q = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .eq("phone_id", call["phone_id"])
        .in_("contact_id", all_ids)
        .order("sent_at")
        .limit(limit)
    )
    if from_ts:
        q = q.gte("sent_at", from_ts)

    msgs = q.execute().data or []

    # ── עדכן סטטוס אם עבר expected_end ──────────────────────────────────────
    call_status = call.get("status", "active")
    expected    = call.get("expected_end")
    if call_status == "active" and expected:
        try:
            exp_dt = datetime.fromisoformat(expected.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                call_status = "completed"
                db.table("calls").update({
                    "status":                 "completed",
                    "ended_at":               datetime.now(timezone.utc).isoformat(),
                    "last_status_updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", call_id).execute()
        except Exception:
            pass

    phone_res    = db.table("phones").select("number").eq("id", call["phone_id"]).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    from routers.messages import format_message
    messages = [format_message(m, phone_number, call["phone_id"]) for m in msgs]

    return {
        "call_id":     call_id,
        "call_status": call_status,
        "expected_end": expected,
        "ended_at":    call.get("ended_at"),
        "messages":    messages,
    }


@router.post("/end")
async def end_call(
    body: EndCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    now = datetime.now(timezone.utc).isoformat()
    res = (
        db.table("calls")
        .update({
            "status":                 body.status,
            "ended_at":               now,
            "last_status_updated_at": now,
        })
        .eq("id", body.call_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Call not found")

    # ── בטל רישום webhook ──────────────────────────────────────────────────
    call_data = res.data[0]
    try:
        db.table("webhook_registrations") \
          .delete() \
          .eq("phone_id",   call_data.get("phone_id", "")) \
          .eq("contact_id", call_data.get("contact_id", "")) \
          .execute()
    except Exception:
        pass

    return {"call_id": body.call_id, "status": body.status, "ended_at": now}


@router.get("/{call_id}")
async def get_call(
    call_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
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