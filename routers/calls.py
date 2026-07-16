# routers/calls.py
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_current_user, get_supabase

router = APIRouter(prefix="/calls", tags=["calls"])
logger = logging.getLogger(__name__)

CALL_TYPE_RECORDING = "recording"
CALL_TYPE_JOB = "job"


class StartCallRequest(BaseModel):
    phone_id: str
    contact_id: str
    scenario_id: str | None = None
    duration_seconds: int = Field(default=300, ge=1, le=86_400)


class EndCallRequest(BaseModel):
    call_id: str
    status: Literal["completed", "failed", "cancelled"] = "completed"


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

    row = res.data[0]
    host = row.get("agent_hosts") or {}
    ip = host.get("ip_address")
    api_port = row.get("api_port")
    number = row.get("number", "")

    logger.info(
        "[CALLS] Agent info: phone_id=%s ip=%s port=%s number=%s",
        phone_id,
        ip,
        api_port,
        number,
    )
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


@router.post("/start")
async def start_call(
    body: StartCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    logger.info(
        "[CALLS][START] phone_id=%s contact_id=%s call_type=%s duration=%s",
        body.phone_id,
        body.contact_id,
        CALL_TYPE_RECORDING,
        body.duration_seconds,
    )

    ip, api_port, _phone_number = await _get_agent_info(db, body.phone_id)
    if not ip or not api_port:
        raise HTTPException(status_code=404, detail="Agent not found for this phone")

    contact_number = await _get_contact_number(db, body.contact_id)
    if not contact_number:
        raise HTTPException(status_code=404, detail="Contact phone number not found")

    now = datetime.now(timezone.utc)
    expected = now + timedelta(seconds=body.duration_seconds)
    call_id = str(uuid.uuid4())

    db.table("calls").insert(
        {
            "id": call_id,
            "phone_id": body.phone_id,
            "contact_id": body.contact_id,
            "scenario_id": body.scenario_id,
            "status": "active",
            "call_type": CALL_TYPE_RECORDING,
            "started_at": now.isoformat(),
            "expected_end": expected.isoformat(),
            "created_at": now.isoformat(),
            "last_status_updated_at": now.isoformat(),
        }
    ).execute()

    logger.info("[CALLS][START] Call created. call_id=%s", call_id)

    # The permanent recording webhook is ensured once during app startup.
    agent_ok = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"http://{ip}:{api_port}/send",
                json={
                    "to": contact_number,
                    "message": "שיחה התחילה",
                    "call_id": call_id,
                },
            )
            agent_ok = response.status_code < 300
            logger.info(
                "[CALLS][START] Agent notified. call_id=%s status=%s",
                call_id,
                response.status_code,
            )
    except Exception as exc:
        logger.error(
            "[CALLS][START] Failed to notify agent. call_id=%s error=%s",
            call_id,
            exc,
        )

    return {
        "call_id": call_id,
        "call_type": CALL_TYPE_RECORDING,
        "status": "active",
        "started_at": now.isoformat(),
        "expected_end": expected.isoformat(),
        "agent_sent": agent_ok,
        "phone_id": body.phone_id,
        "contact_id": body.contact_id,
    }


@router.get("/{call_id}/messages")
async def poll_call_messages(
    call_id: str,
    since: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Read playback messages only. Call expiration is handled by the startup job."""
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
        raise HTTPException(status_code=404, detail="Call not found")

    call = call_res.data[0]
    contact_id = call["contact_id"]

    child_res = (
        db.table("contacts")
        .select("id")
        .eq("parent_contact_id", contact_id)
        .execute()
    )
    child_ids = [child["id"] for child in (child_res.data or [])]
    all_ids = [contact_id, *child_ids]

    from_ts = since or call.get("started_at") or ""

    query = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .eq("phone_id", call["phone_id"])
        .in_("contact_id", all_ids)
        .order("sent_at")
        .limit(limit)
    )
    if from_ts:
        query = query.gte("sent_at", from_ts)

    raw_messages = query.execute().data or []
    logger.info("[CALLS][POLL] call_id=%s messages_found=%d", call_id, len(raw_messages))

    phone_res = (
        db.table("phones")
        .select("number")
        .eq("id", call["phone_id"])
        .limit(1)
        .execute()
    )
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    from routers.messages import format_message

    messages = [
        format_message(message, phone_number, call["phone_id"])
        for message in raw_messages
    ]

    return {
        "call_id": call_id,
        "call_type": call.get("call_type", CALL_TYPE_JOB),
        "call_status": call.get("status", "active"),
        "expected_end": call.get("expected_end"),
        "ended_at": call.get("ended_at"),
        "messages": messages,
    }


@router.post("/end")
async def end_call(
    body: EndCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Orderly end. It never disables the permanent listener webhook."""
    logger.info("[CALLS][END] call_id=%s status=%s", body.call_id, body.status)

    now = datetime.now(timezone.utc).isoformat()
    result = (
        db.table("calls")
        .update(
            {
                "status": body.status,
                "ended_at": now,
                "last_status_updated_at": now,
            }
        )
        .eq("id", body.call_id)
        .eq("status", "active")
        .execute()
    )

    if not result.data:
        existing = (
            db.table("calls")
            .select("id, status, ended_at")
            .eq("id", body.call_id)
            .limit(1)
            .execute()
        )
        if not existing.data:
            logger.warning("[CALLS][END] Call not found. call_id=%s", body.call_id)
            raise HTTPException(status_code=404, detail="Call not found")

        current = existing.data[0]
        return {
            "call_id": body.call_id,
            "status": current.get("status"),
            "ended_at": current.get("ended_at"),
            "already_ended": True,
        }

    logger.info("[CALLS][END] Completed. call_id=%s ended_at=%s", body.call_id, now)
    return {
        "call_id": body.call_id,
        "status": body.status,
        "ended_at": now,
        "already_ended": False,
    }


@router.get("/{call_id}")
async def get_call(
    call_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    logger.info("[CALLS][GET] call_id=%s", call_id)
    result = (
        db.table("calls")
        .select("*")
        .eq("id", call_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        logger.warning("[CALLS][GET] Not found. call_id=%s", call_id)
        raise HTTPException(status_code=404, detail="Call not found")

    return result.data[0]
