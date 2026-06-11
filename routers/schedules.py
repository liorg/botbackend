"""
schedules_router.py  –  FastAPI CRUD for the `schedules` table
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncpg

from db import get_pool          # your existing asyncpg pool helper
from auth import get_current_user  # your existing auth dependency

router = APIRouter(prefix="/schedules", tags=["schedules"])


# ─────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────

class ScheduleBase(BaseModel):
    phone_id:      Optional[UUID] = None
    contact_id:    Optional[UUID] = None
    scenario_id:   Optional[UUID] = None
    schedule_name: Optional[str]  = None
    schedule_type: str                        # hourly | daily | weekly | monthly | once
    status:        Optional[str]  = "ready"  # ready | running | disabled
    run_at:        Optional[datetime] = None
    cron_expr:     Optional[str]  = None
    interval_min:  Optional[int]  = None


class ScheduleCreate(ScheduleBase):
    pass


class ScheduleUpdate(BaseModel):
    """All fields optional for PATCH-style updates."""
    phone_id:      Optional[UUID]     = None
    contact_id:    Optional[UUID]     = None
    scenario_id:   Optional[UUID]     = None
    schedule_name: Optional[str]      = None
    schedule_type: Optional[str]      = None
    status:        Optional[str]      = None
    run_at:        Optional[datetime] = None
    cron_expr:     Optional[str]      = None
    interval_min:  Optional[int]      = None
    next_run:      Optional[datetime] = None


class ScheduleOut(ScheduleBase):
    id:         UUID
    last_run:   Optional[datetime] = None
    next_run:   Optional[datetime] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _row_to_dict(row: asyncpg.Record) -> dict:
    return dict(row)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@router.get("/", response_model=list[ScheduleOut])
async def list_schedules(
    phone_id: Optional[UUID] = Query(None),
    status:   Optional[str]  = Query(None),
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    """List all schedules. Optionally filter by phone_id and/or status."""
    conditions = []
    args: list = []

    if phone_id:
        args.append(phone_id)
        conditions.append(f"phone_id = ${len(args)}")

    if status:
        args.append(status)
        conditions.append(f"status = ${len(args)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT * FROM schedules
        {where}
        ORDER BY created_at DESC
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [_row_to_dict(r) for r in rows]


@router.get("/{schedule_id}", response_model=ScheduleOut)
async def get_schedule(
    schedule_id: UUID,
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM schedules WHERE id = $1", schedule_id
        )
    if not row:
        raise HTTPException(404, "Schedule not found")
    return _row_to_dict(row)


@router.post("/", response_model=ScheduleOut, status_code=201)
async def create_schedule(
    body: ScheduleCreate,
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO schedules
                (phone_id, contact_id, scenario_id, schedule_name, schedule_type,
                 status, run_at, cron_expr, interval_min)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING *
            """,
            body.phone_id,
            body.contact_id,
            body.scenario_id,
            body.schedule_name,
            body.schedule_type,
            body.status or "ready",
            body.run_at,
            body.cron_expr,
            body.interval_min,
        )
    return _row_to_dict(row)


@router.put("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: UUID,
    body: ScheduleUpdate,
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    """Partial update — only non-None fields are written."""
    updates: dict = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")

    set_clauses = ", ".join(
        f"{col} = ${i+2}" for i, col in enumerate(updates.keys())
    )
    values = list(updates.values())

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE schedules
            SET {set_clauses}
            WHERE id = $1
            RETURNING *
            """,
            schedule_id,
            *values,
        )
    if not row:
        raise HTTPException(404, "Schedule not found")
    return _row_to_dict(row)


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: UUID,
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM schedules WHERE id = $1", schedule_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, "Schedule not found")


@router.post("/{schedule_id}/run", response_model=ScheduleOut)
async def run_schedule_now(
    schedule_id: UUID,
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    """Mark schedule as running and set last_run = now."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE schedules
            SET status   = 'running',
                last_run = NOW()
            WHERE id = $1
            RETURNING *
            """,
            schedule_id,
        )
    if not row:
        raise HTTPException(404, "Schedule not found")
    return _row_to_dict(row)


@router.patch("/{schedule_id}/status", response_model=ScheduleOut)
async def set_schedule_status(
    schedule_id: UUID,
    status: str = Query(..., pattern="^(ready|running|disabled)$"),
    pool: asyncpg.Pool = Depends(get_pool),
    user=Depends(get_current_user),
):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE schedules SET status=$2 WHERE id=$1 RETURNING *",
            schedule_id, status,
        )
    if not row:
        raise HTTPException(404, "Schedule not found")
    return _row_to_dict(row)