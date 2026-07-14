# routers/schedules.py
"""
CRUD לתזמונים.

התיקון המרכזי: next_run מחושב בכל create/update/run.
בלעדיו הוא נשאר NULL, ה-scheduler שואל `WHERE next_run <= now()`
ומקבל אפס שורות — כלומר אף תזמון לא יורה לעולם.
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

RECURRING = {"hourly", "daily", "weekly", "monthly"}


# ── Schemas ────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    phone_id:      Optional[str] = None
    contact_id:    Optional[str] = None
    scenario_id:   Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: str                      # hourly | daily | weekly | monthly | once
    status:        Optional[str] = "ready"  # ready | running | disabled
    run_at:        Optional[str] = None
    cron_expr:     Optional[str] = None     # JSON string {hour, intervalHours, days, dayOfMonth}
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
    return q.execute().data or []


# ── Get one ────────────────────────────────────────────────────────────────

@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, db: Client = Depends(get_supabase)):
    res = db.table("schedules").select("*").eq("id", schedule_id) \
            .maybe_single().execute().data
    if not res:
        raise HTTPException(404, "Schedule not found")
    return res


# ── Calls של התזמון — ההפעלות עצמן ─────────────────────────────────────────
# אין ישות "run" נפרדת: כל הפעלה היא call אחד, ושדות ה-summary
# (duration, mismatch, leaves) יושבים עליו.

@router.get("/{schedule_id}/calls")
async def schedule_calls(
    schedule_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_supabase),
):
    return {"calls": db.rpc("spine_schedule_calls", {
        "p_schedule_id": schedule_id,
        "p_limit":       limit,
    }).execute().data or []}


# ── Create ─────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_schedule(body: ScheduleCreate, db: Client = Depends(get_supabase)):
    payload = {
        "id":            str(uuid.uuid4()),
        "schedule_type": body.schedule_type,
        "status":        body.status or "ready",
    }
    for field in ("phone_id", "contact_id", "scenario_id",
                  "schedule_name", "run_at", "cron_expr", "interval_min"):
        val = getattr(body, field)
        if val:
            payload[field] = val

    # ← זה מה שהיה חסר. בלעדיו next_run=NULL והתזמון לא יורה.
    payload["next_run"] = compute_next_run(
        db, body.schedule_type, body.cron_expr, body.run_at)

    res = db.table("schedules").insert(payload).execute()
    if not res.data:
        raise HTTPException(500, "Failed to create schedule")
    return res.data[0]


# ── Update ─────────────────────────────────────────────────────────────────

@router.put("/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleUpdate,
                          db: Client = Depends(get_supabase)):
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(400, "No fields to update")

    # אם השתנה התזמון עצמו — לחשב מחדש. אלא אם הקורא נתן next_run במפורש.
    touches_timing = any(k in payload for k in ("schedule_type", "cron_expr", "run_at"))
    if touches_timing and "next_run" not in payload:
        current = db.table("schedules").select("schedule_type, cron_expr, run_at") \
                    .eq("id", schedule_id).maybe_single().execute().data or {}
        payload["next_run"] = compute_next_run(
            db,
            payload.get("schedule_type") or current.get("schedule_type"),
            payload.get("cron_expr")     or current.get("cron_expr"),
            payload.get("run_at")        or current.get("run_at"),
        )

    res = db.table("schedules").update(payload).eq("id", schedule_id).execute()
    if not res.data:
        raise HTTPException(404, "Schedule not found")
    return res.data[0]


# ── Delete ─────────────────────────────────────────────────────────────────

@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str, db: Client = Depends(get_supabase)):
    db.table("schedules").delete().eq("id", schedule_id).execute()
    return {"ok": True}


# ── Run now ────────────────────────────────────────────────────────────────
# הגרסה הקודמת רק עשתה update status='running' — היא לא הריצה כלום.
# כאן next_run נדחף לעכשיו, וה-scheduler יתפוס אותו ב-claim הבא (≤30ש').
# היתרון על קריאה ישירה ל-/api/calls/ensure: אותו מסלול בדיוק כמו
# תזמון רגיל — אותו claim, אותו close, אותו חישוב next_run.

@router.post("/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, db: Client = Depends(get_supabase)):
    sched = db.table("schedules").select("*").eq("id", schedule_id) \
              .maybe_single().execute().data
    if not sched:
        raise HTTPException(404, "Schedule not found")

    if not (sched.get("scenario_id") and sched.get("contact_id") and sched.get("phone_id")):
        raise HTTPException(400, "Schedule is missing phone/contact/scenario")

    res = db.table("schedules").update({
        "status":   "running",
        "next_run": datetime.now(timezone.utc).isoformat(),   # ← ייתפס ב-claim הבא
    }).eq("id", schedule_id).execute()

    return {**res.data[0], "queued": True,
            "message": "Scheduled to fire on next scheduler tick"}
