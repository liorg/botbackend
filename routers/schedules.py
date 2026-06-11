# routers/schedules.py
from fastapi import APIRouter, Depends, HTTPException, Query
from dependencies import get_supabase
from supabase import Client
from pydantic import BaseModel
from typing import Optional
import uuid

router = APIRouter(prefix="/schedules", tags=["schedules"])


# ── Schemas ────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    phone_id:      Optional[str] = None
    contact_id:    Optional[str] = None
    scenario_id:   Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: str                       # hourly | daily | weekly | monthly | once
    status:        Optional[str] = "ready"  # ready | running | disabled
    run_at:        Optional[str] = None      # ISO string
    cron_expr:     Optional[str] = None
    interval_min:  Optional[int] = None


class ScheduleUpdate(BaseModel):
    phone_id:      Optional[str] = None
    contact_id:    Optional[str] = None
    scenario_id:   Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: Optional[str] = None
    status:        Optional[str] = None
    run_at:        Optional[str] = None
    cron_expr:     Optional[str] = None
    interval_min:  Optional[int] = None
    next_run:      Optional[str] = None


# ── List ───────────────────────────────────────────────────────────────────

@router.get("/")
async def list_schedules(
    phone_id: Optional[str] = Query(None),
    status:   Optional[str] = Query(None),
    db: Client = Depends(get_supabase),
):
    q = db.table("schedules").select("*").order("created_at", desc=True)
    if phone_id:
        q = q.eq("phone_id", phone_id)
    if status:
        q = q.eq("status", status)
    result = q.execute()
    return result.data or []


# ── Get one ────────────────────────────────────────────────────────────────

@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("schedules")
        .select("*")
        .eq("id", schedule_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Schedule not found")
    return result.data


# ── Create ─────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_schedule(body: ScheduleCreate, db: Client = Depends(get_supabase)):
    payload = {
        "id":            str(uuid.uuid4()),
        "schedule_type": body.schedule_type,
        "status":        body.status or "ready",
    }
    if body.phone_id:      payload["phone_id"]      = body.phone_id
    if body.contact_id:    payload["contact_id"]    = body.contact_id
    if body.scenario_id:   payload["scenario_id"]   = body.scenario_id
    if body.schedule_name: payload["schedule_name"] = body.schedule_name
    if body.run_at:        payload["run_at"]         = body.run_at
    if body.cron_expr:     payload["cron_expr"]      = body.cron_expr
    if body.interval_min:  payload["interval_min"]   = body.interval_min

    result = db.table("schedules").insert(payload).execute()
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
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(400, "No fields to update")

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

@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: str, db: Client = Depends(get_supabase)):
    db.table("schedules").delete().eq("id", schedule_id).execute()


# ── Run now ────────────────────────────────────────────────────────────────

@router.post("/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, db: Client = Depends(get_supabase)):
    from datetime import datetime, timezone
    result = (
        db.table("schedules")
        .update({"status": "running", "last_run": datetime.now(timezone.utc).isoformat()})
        .eq("id", schedule_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Schedule not found")
    return result.data[0]