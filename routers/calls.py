# routers/calls.py
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_current_user, get_supabase
from routers.common_schemas import Passthrough
from routers.messages import ChatMessage

router = APIRouter(prefix="/calls", tags=["calls"])
logger = logging.getLogger(__name__)

CALL_TYPE_RECORDING = "recording"
# חייב להיות "active" — כל ה-backend של ההקלטה (callback, expiry worker,
# frontend) מסתנכרן על active. "running" שבר את קישור ההודעות.
RUNNING_STATUS = "active"

TERMINAL_STATUSES = {
    "completed",
    "failed",
    "aborted",
    "expired",
    "timeout",
}


class StartCallRequest(BaseModel):
    phone_id: str = Field(..., description="Phone id.")
    contact_id: str = Field(..., description="Contact id.")
    scenario_id: str | None = Field(None, description="Scenario to link to the call, optional.")
    duration_seconds: int = Field(default=300, ge=1, le=86_400, description="Recording length in seconds (1-86400). Sets expected_end.")


class EndCallRequest(BaseModel):
    call_id: str = Field(..., description="Recording call to end.")
    status: Literal[
        "completed",
        "failed",
        "aborted",
        "expired",
        "timeout",
    ] = Field("completed", description="Final status: completed, failed, aborted, expired or timeout.")


# ── Response Schemas ──────────────────────────────────────────────────────

class RecordingCall(BaseModel):
    id: str = Field(..., description="Call id.")
    phone_id: Optional[str] = Field(None, description="Phone id.")
    contact_id: Optional[str] = Field(None, description="Contact id.")
    scenario_id: Optional[str] = Field(None, description="Scenario id.")
    status: Optional[str] = Field(None, description="Call status; active while recording.")
    call_type: Optional[str] = Field(None, description="Always recording here.")
    started_at: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")
    expected_end: Optional[str] = Field(None, description="When the recording ends automatically. ISO 8601 timestamp, as stored.")


class StartCallResponse(BaseModel):
    call_id: str = Field(..., description="Call id.")
    call_type: str = Field(..., description="Always recording.")
    status: str = Field(..., description="Call status; active while recording.")
    started_at: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")
    expected_end: Optional[str] = Field(None, description="When the recording ends automatically. ISO 8601 timestamp, as stored.")
    phone_id: str = Field(..., description="Phone id.")
    contact_id: str = Field(..., description="Contact id.")
    already_running: bool = Field(..., description="True when an active recording already existed and was returned instead of a new one.")


class ActiveCallResponse(BaseModel):
    active: bool = Field(..., description="True when a recording call is active for the phone and contact.")
    call: Optional[RecordingCall] = Field(None, description="The active recording call, or null.")


class CallMessagesResponse(BaseModel):
    call_id: str = Field(..., description="Call id.")
    call_type: Optional[str] = Field(None, description="Always recording.")
    call_status: Optional[str] = Field(None, description="Current call status.")
    expected_end: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")
    ended_at: Optional[str] = Field(None, description="When the call ended; null while active.")
    messages: list[ChatMessage] = Field(..., description="Messages of the call, oldest first, formatted for the chat view.")


class EndCallResponse(BaseModel):
    call_id: str = Field(..., description="Call id.")
    status: Optional[str] = Field(None, description="Final call status.")
    ended_at: Optional[str] = Field(None, description="ISO 8601 timestamp, as stored.")
    already_ended: bool = Field(..., description="True when the call was not active, so nothing changed.")


class CallRow(Passthrough):
    """Full calls row (select("*")); every column is kept."""
    id: str = Field(..., description="Call id.")
    phone_id: Optional[str] = Field(None, description="Phone id.")
    contact_id: Optional[str] = Field(None, description="Contact id.")
    scenario_id: Optional[str] = Field(None, description="Scenario id.")
    status: Optional[str] = Field(None, description="Call status.")
    call_type: Optional[str] = Field(None, description="recording or a scenario call type.")


def _get_running_recording_call(
    db: Client,
    phone_id: str,
    contact_id: str,
) -> dict | None:
    result = (
        db.table("calls")
        .select(
            "id, phone_id, contact_id, scenario_id, status, "
            "call_type, started_at, expected_end"
        )
        .eq("phone_id", phone_id)
        .eq("contact_id", contact_id)
        .eq("call_type", CALL_TYPE_RECORDING)
        .eq("status", RUNNING_STATUS)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    return result.data[0] if result.data else None


@router.post("/start", response_model=StartCallResponse, summary="Start recording call", description="Starts a recording call for the phone and contact, ending after duration_seconds. If one is already active, returns it with already_running=true.")
def start_call(
    body: StartCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    logger.info(
        "[CALLS][START] phone=%s contact=%s type=%s duration=%s",
        body.phone_id,
        body.contact_id,
        CALL_TYPE_RECORDING,
        body.duration_seconds,
    )

    existing = _get_running_recording_call(
        db,
        body.phone_id,
        body.contact_id,
    )

    if existing:
        return {
            "call_id": existing["id"],
            "call_type": CALL_TYPE_RECORDING,
            "status": existing["status"],
            "started_at": existing.get("started_at"),
            "expected_end": existing.get("expected_end"),
            "phone_id": existing["phone_id"],
            "contact_id": existing["contact_id"],
            "already_running": True,
        }

    now = datetime.now(timezone.utc)
    expected_end = now + timedelta(seconds=body.duration_seconds)
    call_id = str(uuid.uuid4())

    result = (
        db.table("calls")
        .insert(
            {
                "id": call_id,
                "phone_id": body.phone_id,
                "contact_id": body.contact_id,
                "scenario_id": body.scenario_id,
                "status": RUNNING_STATUS,
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
        raise HTTPException(
            status_code=500,
            detail="Failed creating recording call",
        )

    return {
        "call_id": call_id,
        "call_type": CALL_TYPE_RECORDING,
        "status": RUNNING_STATUS,
        "started_at": now.isoformat(),
        "expected_end": expected_end.isoformat(),
        "phone_id": body.phone_id,
        "contact_id": body.contact_id,
        "already_running": False,
    }


# חשוב: חייב להיות לפני @router.get("/{call_id}") כדי ש-"active"
# לא ייתפס כ-call_id.
@router.get("/active/{phone_id}/{contact_id}", response_model=ActiveCallResponse, summary="Get active recording call", description="Returns the active recording call for the phone and contact, if any.")
def get_active_call(
    phone_id: str,
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    call = _get_running_recording_call(db, phone_id, contact_id)
    return {"active": bool(call), "call": call}


@router.get("/{call_id}/messages", response_model=CallMessagesResponse, summary="Poll recording call messages", description="Returns the messages linked to the recording call, oldest first. Send since=<sent_at> to get only newer messages.")
def poll_call_messages(
    call_id: str,
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
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
        query = query.gt("sent_at", since)

    raw_messages = query.execute().data or []

    phone_result = (
        db.table("phones")
        .select("number")
        .eq("id", call["phone_id"])
        .limit(1)
        .execute()
    )

    phone_number = (phone_result.data or [{}])[0].get("number", "")

    from routers.messages import format_message

    messages = [
        format_message(message, phone_number, call["phone_id"])
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


@router.post("/end", response_model=EndCallResponse, summary="End recording call", description="Ends an active recording call with the given status. If it already ended, returns its current state with already_ended=true.")
def end_call(
    body: EndCallRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
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
        .eq("status", RUNNING_STATUS)
        .execute()
    )

    if not result.data:
        existing = (
            db.table("calls")
            .select("id, call_type, status, ended_at")
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

    return {
        "call_id": body.call_id,
        "status": body.status,
        "ended_at": now,
        "already_ended": False,
    }


@router.get("/{call_id}", response_model=CallRow, summary="Get call", description="Returns the full calls row.")
def get_call(
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
