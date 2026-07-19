# routers/calls.py

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

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
    duration_seconds: int = Field(
        default=300,
        ge=1,
        le=86_400,
    )


class EndCallRequest(BaseModel):
    call_id: str

    status: Literal[
        "completed",
        "failed",
        "cancelled",
    ] = "completed"


def _validate_phone(
    db: Client,
    phone_id: str,
) -> None:
    result = (
        db.table("phones")
        .select("id")
        .eq("id", phone_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="Phone not found",
        )


def _validate_contact(
    db: Client,
    phone_id: str,
    contact_id: str,
) -> None:
    result = (
        db.table("contacts")
        .select("id")
        .eq("id", contact_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="Contact not found for supplied phone",
        )


def _get_active_recording_call(
    db: Client,
    phone_id: str,
    contact_id: str,
) -> dict | None:
    """
    מונע פתיחת יותר מ-recording call פעיל אחד
    לאותו phone/contact.
    """

    result = (
        db.table("calls")
        .select(
            "id, phone_id, contact_id, scenario_id, status, "
            "call_type, started_at, expected_end"
        )
        .eq("phone_id", phone_id)
        .eq("contact_id", contact_id)
        .eq("call_type", CALL_TYPE_RECORDING)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    return result.data[0] if result.data else None


@router.post("/start")
async def start_call(
    body: StartCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    פותח חלון הקלטה.

    אין כאן שליחה ל-HostAgent.
    ה-recording webhook כבר נרשם באופן גלובלי ב-main.py.

    הודעות שמגיעות בזמן שה-call פעיל יקושרו אליו
    על ידי webhook_registrations.callback.
    """

    logger.info(
        "[CALLS][START] phone=%s contact=%s type=%s duration=%s",
        body.phone_id,
        body.contact_id,
        CALL_TYPE_RECORDING,
        body.duration_seconds,
    )

    _validate_phone(
        db,
        body.phone_id,
    )

    _validate_contact(
        db,
        body.phone_id,
        body.contact_id,
    )

    existing = _get_active_recording_call(
        db,
        body.phone_id,
        body.contact_id,
    )

    if existing:
        logger.info(
            "[CALLS][START] active recording already exists | call=%s",
            existing["id"],
        )

        return {
            "call_id": existing["id"],
            "call_type": CALL_TYPE_RECORDING,
            "status": existing["status"],
            "started_at": existing.get("started_at"),
            "expected_end": existing.get("expected_end"),
            "phone_id": existing["phone_id"],
            "contact_id": existing["contact_id"],
            "already_active": True,
        }

    now = datetime.now(timezone.utc)

    expected_end = now + timedelta(
        seconds=body.duration_seconds
    )

    call_id = str(uuid.uuid4())

    result = (
        db.table("calls")
        .insert(
            {
                "id": call_id,
                "phone_id": body.phone_id,
                "contact_id": body.contact_id,
                "scenario_id": body.scenario_id,
                "status": "active",
                "call_type": CALL_TYPE_RECORDING,
                "started_at": now.isoformat(),
                "expected_end": expected_end.isoformat(),
                "created_at": now.isoformat(),
                "last_status_updated_at": now.isoformat(),
            }
        )
        .execute()
    )

    if not result.data:
        logger.error(
            "[CALLS][START] insert returned no data | call=%s",
            call_id,
        )

        raise HTTPException(
            status_code=500,
            detail="Failed creating recording call",
        )

    logger.info(
        "[CALLS][START] recording call created | call=%s",
        call_id,
    )

    return {
        "call_id": call_id,
        "call_type": CALL_TYPE_RECORDING,
        "status": "active",
        "started_at": now.isoformat(),
        "expected_end": expected_end.isoformat(),
        "phone_id": body.phone_id,
        "contact_id": body.contact_id,
        "already_active": False,
    }


@router.get("/{call_id}/messages")
async def poll_call_messages(
    call_id: str,
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    מחזיר רק הודעות שקושרו ל-recording call המסוים.

    הקישור messages.call_id נוצר ב-recording callback.
    אין חיפוש כללי לפי contact/time, כדי שלא לערבב שיחות.
    """

    logger.info(
        "[CALLS][POLL] call=%s since=%s limit=%s",
        call_id,
        since,
        limit,
    )

    call_result = (
        db.table("calls")
        .select(
            "id, phone_id, contact_id, started_at, expected_end, "
            "status, ended_at, call_type"
        )
        .eq("id", call_id)
        .eq("call_type", CALL_TYPE_RECORDING)
        .limit(1)
        .execute()
    )

    if not call_result.data:
        logger.warning(
            "[CALLS][POLL] recording call not found | call=%s",
            call_id,
        )

        raise HTTPException(
            status_code=404,
            detail="Recording call not found",
        )

    call = call_result.data[0]

    query = (
        db.table("messages")
        .select(
            "id, whatsapp_message_id, call_id, contact_id, phone_id, "
            "sender, content, sent_at, direction, media_url"
        )
        .eq("call_id", call_id)
        .order("sent_at")
        .limit(limit)
    )

    if since:
        query = query.gt(
            "sent_at",
            since,
        )

    raw_messages = query.execute().data or []

    logger.info(
        "[CALLS][POLL] call=%s messages=%s",
        call_id,
        len(raw_messages),
    )

    phone_result = (
        db.table("phones")
        .select("number")
        .eq("id", call["phone_id"])
        .limit(1)
        .execute()
    )

    phone_number = (
        phone_result.data or [{}]
    )[0].get("number", "")

    from routers.messages import format_message

    messages = [
        format_message(
            message,
            phone_number,
            call["phone_id"],
        )
        for message in raw_messages
    ]

    return {
        "call_id": call_id,
        "call_type": call["call_type"],
        "call_status": call["status"],
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
    """
    סוגר recording call.

    לא מוחק ולא מכבה את ה-recording webhook הגלובלי.
    """

    logger.info(
        "[CALLS][END] call=%s status=%s",
        body.call_id,
        body.status,
    )

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
        .eq("call_type", CALL_TYPE_RECORDING)
        .eq("status", "active")
        .execute()
    )

    if not result.data:
        existing = (
            db.table("calls")
            .select(
                "id, call_type, status, ended_at"
            )
            .eq("id", body.call_id)
            .limit(1)
            .execute()
        )

        if not existing.data:
            raise HTTPException(
                status_code=404,
                detail="Call not found",
            )

        current = existing.data[0]

        if current.get("call_type") != CALL_TYPE_RECORDING:
            raise HTTPException(
                status_code=409,
                detail="Call is not a recording call",
            )

        return {
            "call_id": body.call_id,
            "status": current.get("status"),
            "ended_at": current.get("ended_at"),
            "already_ended": True,
        }

    logger.info(
        "[CALLS][END] recording ended | call=%s ended_at=%s",
        body.call_id,
        now,
    )

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
    result = (
        db.table("calls")
        .select("*")
        .eq("id", call_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="Call not found",
        )

    return result.data[0]
