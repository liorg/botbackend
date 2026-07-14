# routers/schedules.py
"""
CRUD לתזמונים.

next_run מחושב בכל create/update/run.
בלעדיו הוא נשאר NULL, וה-scheduler לא ימצא תזמונים להרצה.
"""

from datetime import datetime, timezone
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from supabase import Client

from dependencies import get_supabase
from services.scheduler import compute_next_run


router = APIRouter(prefix="/schedules", tags=["schedules"])


# ── Schemas ────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    phone_id: Optional[str] = None
    contact_id: Optional[str] = None
    scenario_id: Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: str
    status: Optional[str] = "ready"
    run_at: Optional[str] = None
    cron_expr: Optional[str] = None
    interval_min: Optional[int] = None


class ScheduleUpdate(BaseModel):
    phone_id: Optional[str] = None
    contact_id: Optional[str] = None
    scenario_id: Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: Optional[str] = None
    status: Optional[str] = None
    run_at: Optional[str] = None
    cron_expr: Optional[str] = None
    interval_min: Optional[int] = None
    next_run: Optional[str] = None


# ── List ───────────────────────────────────────────────────────────────────

@router.get("/")
async def list_schedules(
    phone_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Client = Depends(get_supabase),
):
    query = (
        db.table("schedules")
        .select("*")
        .order("created_at", desc=True)
    )

    if phone_id:
        query = query.eq("phone_id", phone_id)

    if status:
        query = query.eq("status", status)

    return query.execute().data or []


# ── Get one ────────────────────────────────────────────────────────────────

@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("schedules")
        .select("*")
        .eq("id", schedule_id)
        .maybe_single()
        .execute()
        .data
    )

    if not result:
        raise HTTPException(404, "Schedule not found")

    return result


# ── Calls ──────────────────────────────────────────────────────────────────

@router.get("/{schedule_id}/calls")
async def schedule_calls(
    schedule_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_supabase),
):
    calls = db.rpc(
        "spine_schedule_calls",
        {
            "p_schedule_id": schedule_id,
            "p_limit": limit,
        },
    ).execute().data or []

    return {"calls": calls}


# ── Create ─────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_schedule(
    body: ScheduleCreate,
    db: Client = Depends(get_supabase),
):
    payload = {
        "id": str(uuid.uuid4()),
        "schedule_type": body.schedule_type,
        "status": body.status or "ready",
    }

    for field in (
        "phone_id",
        "contact_id",
        "scenario_id",
        "schedule_name",
        "run_at",
        "cron_expr",
        "interval_min",
    ):
        value = getattr(body, field)

        if value is not None:
            payload[field] = value

    payload["next_run"] = compute_next_run(
        body.schedule_type,
        body.cron_expr,
        body.run_at,
    )

    result = (
        db.table("schedules")
        .insert(payload)
        .execute()
    )

    if not result.data:
        raise HTTPException(500, "Failed to create schedule")

    return result.data[0]


# ── Update ─────────────────────────────────────────────────────────────────

@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    db: Client = Depends(get_supabase),
):
    payload = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None
    }

    if not payload:
        raise HTTPException(400, "No fields to update")

    timing_fields = {"schedule_type", "cron_expr", "run_at"}
    touches_timing = any(field in payload for field in timing_fields)

    if touches_timing and "next_run" not in payload:
        current = (
            db.table("schedules")
            .select("schedule_type, cron_expr, run_at")
            .eq("id", schedule_id)
            .maybe_single()
            .execute()
            .data
        )

        if not current:
            raise HTTPException(404, "Schedule not found")

        schedule_type = payload.get(
            "schedule_type",
            current.get("schedule_type"),
        )

        cron_expr = payload.get(
            "cron_expr",
            current.get("cron_expr"),
        )

        run_at = payload.get(
            "run_at",
            current.get("run_at"),
        )

        payload["next_run"] = compute_next_run(
            schedule_type,
            cron_expr,
            run_at,
        )

    result = (
        db.table("schedules")
        .update(payload)
        .eq("id", schedule_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(404, "Schedule not found")

    return result.data[0]


# ── Delete ─────────────────────────────────────────────────────────────────

@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("schedules")
        .delete()
        .eq("id", schedule_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(404, "Schedule not found")

    return {"ok": True}


# ── Run now ────────────────────────────────────────────────────────────────

@router.post("/{schedule_id}/run")
async def run_schedule_now(
    schedule_id: str,
    db: Client = Depends(get_supabase),
):
    schedule = (
        db.table("schedules")
        .select("*")
        .eq("id", schedule_id)
        .maybe_single()
        .execute()
        .data
    )

    if not schedule:
        raise HTTPException(404, "Schedule not found")

    if not (
        schedule.get("scenario_id")
        and schedule.get("contact_id")
        and schedule.get("phone_id")
    ):
        raise HTTPException(
            400,
            "Schedule is missing phone/contact/scenario",
        )

    result = (
        db.table("schedules")
        .update(
            {
                "status": "running",
                "next_run": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", schedule_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(500, "Failed to queue schedule")

    return {
        **result.data[0],
        "queued": True,
        "message": "Scheduled to fire on next scheduler tick",
    }
