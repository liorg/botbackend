# routers/schedules.py
"""
CRUD לתזמונים — מיושר מול ה-Scheduler העצמאי.

חוזה משותף (חובה שיהיה זהה בשני הצדדים):

    עמודות       next_run / last_run   (שמות הסכמה הקיימת — לא משנים סכמה)
    סטטוסים      active / paused / firing / completed / error
    schedule_type once / cron
    cron_expr    ביטוי Linux cron אמיתי ("30 20 * * 0,3")

next_run מחושב בכל create/update/run.
בלעדיו הוא נשאר NULL, וה-Scheduler (שמסנן lte("next_run", now))
לא ימצא את התזמון לעולם.
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

# עמודות מפורשות — בלי select("*").
# עמודה חדשה ב-DB לא זולגת ל-API עד שמוסיפים אותה כאן במודע.
SCHEDULE_COLUMNS = (
    "id,"
    "user_id,"
    "phone_id,"
    "contact_id,"
    "scenario_id,"
    "schedule_name,"
    "schedule_type,"
    "cron_expr,"
    "run_at,"
    "next_run,"
    "last_run,"
    "status,"
    "created_at,"
    "updated_at,"
    "scenarios(name)"
)

VALID_TYPES    = {"once", "cron"}
# סטטוסים שהלקוח רשאי לקבוע. firing מנוהל ע"י ה-Scheduler בלבד.
CLIENT_STATUSES = {"active", "paused"}


# ── Schemas ────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    phone_id: Optional[str] = None
    contact_id: Optional[str] = None
    scenario_id: Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: str                      # once | cron
    status: Optional[str] = "active"
    run_at: Optional[str] = None            # ל-once
    cron_expr: Optional[str] = None         # Linux cron ל-cron


class ScheduleUpdate(BaseModel):
    phone_id: Optional[str] = None
    contact_id: Optional[str] = None
    scenario_id: Optional[str] = None
    schedule_name: Optional[str] = None
    schedule_type: Optional[str] = None
    status: Optional[str] = None
    run_at: Optional[str] = None
    cron_expr: Optional[str] = None


# ── Validation helpers ─────────────────────────────────────────────────────

def _resolve_next_run(
    schedule_type: str,
    cron_expr: Optional[str],
    run_at: Optional[str],
) -> str:
    """
    ולידציה + חישוב next_run בפעולה אחת.

    compute_next_run מחזיר None על ביטוי Cron לא תקין או run_at לא תקין,
    כך שאין צורך בפונקציית ולידציה נפרדת — None כאן הוא תמיד שגיאת קלט.
    """

    if schedule_type not in VALID_TYPES:
        raise HTTPException(400, f"schedule_type must be one of {sorted(VALID_TYPES)}")

    if schedule_type == "cron" and not (cron_expr and cron_expr.strip()):
        raise HTTPException(400, "cron_expr is required for schedule_type=cron")

    if schedule_type == "once" and not run_at:
        raise HTTPException(400, "run_at is required for schedule_type=once")

    next_run = compute_next_run(schedule_type, cron_expr, run_at)

    if next_run is None:
        detail = (
            f"Invalid cron expression: '{cron_expr}'"
            if schedule_type == "cron"
            else f"Invalid run_at: '{run_at}'"
        )
        raise HTTPException(400, detail)

    return next_run


# ── List ───────────────────────────────────────────────────────────────────

@router.get("/")
async def list_schedules(
    phone_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Client = Depends(get_supabase),
):
    query = (
        db.table("schedules")
        .select(SCHEDULE_COLUMNS)
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
        .select(SCHEDULE_COLUMNS)
        .eq("id", schedule_id)
        .maybe_single()
        .execute()
        .data
    )

    if not result:
        raise HTTPException(404, "Schedule not found")

    return result


# ── Calls (לוג אירועים) ────────────────────────────────────────────────────

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
    next_run = _resolve_next_run(
        body.schedule_type,
        body.cron_expr,
        body.run_at,
    )

    status = body.status or "active"
    if status not in CLIENT_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(CLIENT_STATUSES)}")

    payload = {
        "id": str(uuid.uuid4()),
        "schedule_type": body.schedule_type,
        "status": status,
    }

    for field in (
        "phone_id",
        "contact_id",
        "scenario_id",
        "schedule_name",
        "run_at",
        "cron_expr",
    ):
        value = getattr(body, field)
        if value is not None:
            payload[field] = value

    payload["next_run"] = next_run

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

    if "status" in payload and payload["status"] not in CLIENT_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(CLIENT_STATUSES)}")

    timing_fields = {"schedule_type", "cron_expr", "run_at"}
    touches_timing = any(field in payload for field in timing_fields)

    if touches_timing:
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

        schedule_type = payload.get("schedule_type", current.get("schedule_type"))
        cron_expr     = payload.get("cron_expr",     current.get("cron_expr"))
        run_at        = payload.get("run_at",        current.get("run_at"))

        payload["next_run"] = _resolve_next_run(schedule_type, cron_expr, run_at)

    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

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
    """
    הרצה ידית: next_run=now + status=active.

    לא נוגעים ב-status='firing' — זה שייך ל-Scheduler בלבד.
    ה-Scheduler שולף status='active' עם next_run<=now בסבב הבא (≤30ש'),
    יורה לכל אנשי הקשר עם tag='active', ומחשב לבד את ה-next_run הבא.
    """

    # רק מה שהבדיקות צריכות — לא מושכים את כל השורה
    schedule = (
        db.table("schedules")
        .select("id, status, phone_id, scenario_id")
        .eq("id", schedule_id)
        .maybe_single()
        .execute()
        .data
    )

    if not schedule:
        raise HTTPException(404, "Schedule not found")

    if schedule.get("status") == "firing":
        raise HTTPException(409, "Schedule is currently firing")

    if not (schedule.get("scenario_id") and schedule.get("phone_id")):
        raise HTTPException(400, "Schedule is missing phone/scenario")

    now = datetime.now(timezone.utc).isoformat()

    result = (
        db.table("schedules")
        .update(
            {
                "status": "active",
                "next_run": now,
                "updated_at": now,
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
