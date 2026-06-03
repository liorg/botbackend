# routers/calls.py
from fastapi import APIRouter, Depends, HTTPException, Query
from dependencies import get_supabase, get_current_user
from supabase import Client
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import httpx, os, uuid, logging

router = APIRouter(prefix="/calls", tags=["calls"])
logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "https://vid.michal-solutions.com/api")
#journalctl -u fastapi.service -n 50 --no-pager | grep -E "(calls|ERROR|error|500|422|phone)"
CALL_TYPE_RECORDING = "recording"
CALL_TYPE_SCHEDULER = "scheduler"


class StartCallRequest(BaseModel):
    phone_id:         str
    contact_id:       str
    scenario_id:      str | None = None
    duration_seconds: int = 300
    call_type:        str = CALL_TYPE_RECORDING


class EndCallRequest(BaseModel):
    call_id: str
    status:  str = "completed"


def _build_callback_url(phone_id: str, contact_id: str) -> str:
    return f"{BACKEND_URL}/webhook-registrations/callback/{phone_id}/{contact_id}"


async def _get_agent_info(db: Client, phone_id: str):
    res = (
        db.table("phones")
        .select("number, api_port, agent_hosts(ip_address)")
        .eq("id", phone_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        logger.warning("[CALLS] Agent not found for phone_id=%s", phone_id)
        return None, None, ""
    row      = res.data[0]
    host     = row.get("agent_hosts") or {}
    ip       = host.get("ip_address")
    api_port = row.get("api_port")
    number   = row.get("number", "")
    logger.info("[CALLS] Agent info: phone_id=%s ip=%s port=%s number=%s", phone_id, ip, api_port, number)
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
        logger.warning("[CALLS] Contact not found: contact_id=%s", contact_id)
        return ""
    number = res.data[0].get("number", "")
    logger.info("[CALLS] Contact number: contact_id=%s number=%s", contact_id, number)
    return number


def _upsert_webhook(db: Client, callback_url: str, call_type: str, now: datetime):
    existing = (
        db.table("webhook_registrations")
        .select("id")
        .eq("callback_url", callback_url)
        .eq("type",         call_type)
        .eq("is_active",    True)
        .execute()
    )
    if existing.data:
        logger.info("[WEBHOOK] Already active — skipping upsert. url=%s type=%s", callback_url, call_type)
        return

    db.table("webhook_registrations") \
      .update({"is_active": False}) \
      .eq("callback_url", callback_url) \
      .eq("type",         call_type) \
      .eq("is_active",    True) \
      .execute()

    db.table("webhook_registrations").insert({
        "id":           str(uuid.uuid4()),
        "callback_url": callback_url,
        "type":         call_type,
        "status":       "active",
        "is_active":    True,
        "created_at":   now.isoformat(),
    }).execute()
    logger.info("[WEBHOOK] Registered. url=%s type=%s", callback_url, call_type)


def _deactivate_webhook(db: Client, callback_url: str):
    try:
        db.table("webhook_registrations") \
          .update({"is_active": False}) \
          .eq("callback_url", callback_url) \
          .execute()
        logger.info("[WEBHOOK] Deactivated. url=%s", callback_url)
    except Exception as e:
        logger.error("[WEBHOOK] Failed to deactivate. url=%s error=%s", callback_url, e)


@router.post("/start")
async def start_call(
    body: StartCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    logger.info("[CALLS][START] phone_id=%s contact_id=%s call_type=%s duration=%s",
        body.phone_id, body.contact_id, body.call_type, body.duration_seconds)

    if body.call_type not in (CALL_TYPE_RECORDING, CALL_TYPE_SCHEDULER):
        logger.warning("[CALLS][START] Invalid call_type=%s", body.call_type)
        raise HTTPException(400, f"Invalid call_type: {body.call_type}")

    ip, api_port, phone_number = await _get_agent_info(db, body.phone_id)
    if not ip or not api_port:
        raise HTTPException(404, "Agent not found for this phone")

    contact_number = await _get_contact_number(db, body.contact_id)
    if not contact_number:
        raise HTTPException(404, "Contact phone number not found")

    now      = datetime.now(timezone.utc)
    expected = now + timedelta(seconds=body.duration_seconds)
    call_id  = str(uuid.uuid4())

    db.table("calls").insert({
        "id":                     call_id,
        "phone_id":               body.phone_id,
        "contact_id":             body.contact_id,
        "scenario_id":            body.scenario_id,
        "status":                 "active",
        "call_type":              body.call_type,
        "started_at":             now.isoformat(),
        "expected_end":           expected.isoformat(),
        "created_at":             now.isoformat(),
        "last_status_updated_at": now.isoformat(),
    }).execute()
    logger.info("[CALLS][START] Call created. call_id=%s", call_id)

    callback_url = _build_callback_url(body.phone_id, body.contact_id)
    _upsert_webhook(db, callback_url, body.call_type, now)

    agent_ok = False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"http://{ip}:{api_port}/send",
                json={"to": contact_number, "message": "שיחה התחילה", "call_id": call_id},
            )
            agent_ok = r.status_code < 300
            logger.info("[CALLS][START] Agent notified. call_id=%s status=%s", call_id, r.status_code)
    except Exception as e:
        logger.error("[CALLS][START] Failed to notify agent. call_id=%s error=%s", call_id, e)

    return {
        "call_id":      call_id,
        "call_type":    body.call_type,
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
    logger.info("[CALLS][POLL] call_id=%s since=%s limit=%s", call_id, since, limit)

    call_res = (
        db.table("calls")
        .select("id, phone_id, contact_id, started_at, expected_end, status, ended_at, call_type")
        .eq("id", call_id)
        .limit(1)
        .execute()
    )
    if not call_res.data:
        logger.warning("[CALLS][POLL] Call not found. call_id=%s", call_id)
        raise HTTPException(404, "Call not found")

    call = call_res.data[0]

    contact_id = call["contact_id"]
    child_res  = db.table("contacts").select("id").eq("parent_contact_id", contact_id).execute()
    child_ids  = [c["id"] for c in (child_res.data or [])]
    all_ids    = [contact_id] + child_ids

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
    logger.info("[CALLS][POLL] call_id=%s messages_found=%d", call_id, len(msgs))

    call_status = call.get("status", "active")
    expected    = call.get("expected_end")
    if call_status == "active" and expected:
        try:
            exp_dt = datetime.fromisoformat(expected.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                logger.info("[CALLS][POLL] Call expired — marking completed. call_id=%s", call_id)
                call_status = "completed"
                db.table("calls").update({
                    "status":                 "completed",
                    "ended_at":               datetime.now(timezone.utc).isoformat(),
                    "last_status_updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", call_id).execute()
                _deactivate_webhook(db, _build_callback_url(call["phone_id"], call["contact_id"]))
        except Exception as e:
            logger.error("[CALLS][POLL] Error checking expiry. call_id=%s error=%s", call_id, e)

    phone_res    = db.table("phones").select("number").eq("id", call["phone_id"]).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    from routers.messages import format_message
    messages = [format_message(m, phone_number, call["phone_id"]) for m in msgs]

    return {
        "call_id":      call_id,
        "call_type":    call.get("call_type", CALL_TYPE_RECORDING),
        "call_status":  call_status,
        "expected_end": expected,
        "ended_at":     call.get("ended_at"),
        "messages":     messages,
    }


@router.post("/end")
async def end_call(
    body: EndCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    logger.info("[CALLS][END] call_id=%s status=%s", body.call_id, body.status)

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
        logger.warning("[CALLS][END] Call not found. call_id=%s", body.call_id)
        raise HTTPException(404, "Call not found")

    call_data = res.data[0]
    _deactivate_webhook(db, _build_callback_url(
        call_data.get("phone_id", ""),
        call_data.get("contact_id", ""),
    ))
    logger.info("[CALLS][END] ✓ call_id=%s ended_at=%s", body.call_id, now)

    return {"call_id": body.call_id, "status": body.status, "ended_at": now}


@router.get("/{call_id}")
async def get_call(
    call_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    logger.info("[CALLS][GET] call_id=%s", call_id)
    res = (
        db.table("calls")
        .select("*")
        .eq("id", call_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        logger.warning("[CALLS][GET] Not found. call_id=%s", call_id)
        raise HTTPException(404, "Call not found")
    return res.data[0]