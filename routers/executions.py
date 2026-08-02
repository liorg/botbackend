# routers/executions.py
#
# מודול ביצועים מקבילים — הרצת תרחיש מכמה טלפונים מול איש קשר אחד.
# מפתח ייחודי: contact_phone_number ("972...").
#
# רישום ב-main.py:
#   from routers import executions
#   app.include_router(executions.router, prefix="/api")

import os
from datetime import datetime
from typing import Optional, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from supabase import Client

from dependencies import get_supabase

router = APIRouter(prefix="/executions", tags=["executions"])

SPINE_URL = os.getenv("SPINE_URL", "http://scenario_data-spine:8000")

VALID_RUN_MODES = {"once", "manual"}


# ── Schemas ────────────────────────────────────────────────────────────────

class ExecutionTarget(BaseModel):
    phone_id: str
    contact_id: str


class ExecutionCreate(BaseModel):
    name: str                              # "{שם תרחיש} — {DD/MM/YYYY}"
    contact_phone_number: str              # "972xxxxxxxxx"
    source_scenario_id: str
    run_mode: str                          # once | manual
    run_at: Optional[str] = None           # ISO, חובה ב-once
    targets: List[ExecutionTarget]


class ScenariosQuery(BaseModel):
    phone_ids: List[str]


# ── Helpers ────────────────────────────────────────────────────────────────

def _validate_phone_number(value: str) -> str:
    v = value.strip().lstrip("+")
    if not (v.isdigit() and v.startswith("972") and 11 <= len(v) <= 13):
        raise HTTPException(400, "contact_phone_number must be like 972xxxxxxxxx")
    return v


# ── List ───────────────────────────────────────────────────────────────────

@router.get("/")
async def list_executions(db: Client = Depends(get_supabase)):
    res = db.rpc("exec_list", {}).execute()
    return res.data if res.data is not None else []


# ── Seek contact (Wizard שלב 1→2) ─────────────────────────────────────────

@router.get("/seek/{phone_number}")
async def seek_contact(phone_number: str, db: Client = Depends(get_supabase)):
    pn = _validate_phone_number(phone_number)
    res = db.rpc("exec_seek_contact", {"p_phone_number": pn}).execute()
    data = res.data or {}
    if not data.get("phones"):
        raise HTTPException(404, f"No active contact found for {pn}")
    return data


# ── Scenarios for selected phones (Wizard שלב 3) ──────────────────────────

@router.post("/scenarios")
async def scenarios_for_phones(body: ScenariosQuery,
                               db: Client = Depends(get_supabase)):
    if not body.phone_ids:
        raise HTTPException(400, "phone_ids is required")
    res = db.rpc("exec_list_scenarios", {"p_phone_ids": body.phone_ids}).execute()
    return res.data if res.data is not None else []


# ── Create (Wizard שלב 4 — סיום) ──────────────────────────────────────────

@router.post("/")
async def create_execution(body: ExecutionCreate,
                           db: Client = Depends(get_supabase)):
    pn = _validate_phone_number(body.contact_phone_number)

    if body.run_mode not in VALID_RUN_MODES:
        raise HTTPException(400, f"run_mode must be one of {sorted(VALID_RUN_MODES)}")
    if body.run_mode == "once" and not body.run_at:
        raise HTTPException(400, "run_at is required for run_mode=once")
    if not body.targets:
        raise HTTPException(400, "targets is required")

    # unique key — הודעת שגיאה ברורה לפני הפגיעה ב-constraint
    existing = (db.table("executions")
                  .select("id")
                  .eq("contact_phone_number", pn)
                  .execute())
    if existing.data:
        raise HTTPException(409, f"Execution already exists for {pn}")

    params = {
        "p_name": body.name,
        "p_contact_phone_number": pn,
        "p_source_scenario_id": body.source_scenario_id,
        "p_run_mode": body.run_mode,
        "p_run_at": body.run_at,
        "p_targets": [t.model_dump() for t in body.targets],
    }
    try:
        res = db.rpc("exec_create", params).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"exec_create failed: {exc}")
    return res.data


# ── Run (כפתור ▶ הפעל — דיאלוג אישור בצד לקוח) ───────────────────────────

@router.post("/{execution_id}/run")
async def run_execution(execution_id: str,
                        db: Client = Depends(get_supabase)):
    links_res = db.rpc("exec_links", {"p_execution_id": execution_id}).execute()
    links = links_res.data or []
    if not links:
        raise HTTPException(404, "Execution has no links")

    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for link in links:
            payload = {
                "phone_id": link["phone_id"],
                "contact_id": link["contact_id"],
                "scenario_id": link["scenario_id"],
                "source": "execution",
            }
            try:
                r = await client.post(f"{SPINE_URL}/api/spine/dispatch",
                                      json=payload)
                results.append({"phone_id": link["phone_id"],
                                "status_code": r.status_code})
            except httpx.HTTPError as exc:
                results.append({"phone_id": link["phone_id"],
                                "error": str(exc)})

    db.table("executions").update({"status": "running"}) \
        .eq("id", execution_id).execute()

    return {"execution_id": execution_id, "dispatched": results}


# ── Stop ───────────────────────────────────────────────────────────────────

@router.post("/{execution_id}/stop")
async def stop_execution(execution_id: str,
                         db: Client = Depends(get_supabase)):
    links_res = db.rpc("exec_links", {"p_execution_id": execution_id}).execute()
    for link in (links_res.data or []):
        if link.get("schedule_id"):
            db.table("schedules").update({"status": "paused"}) \
                .eq("id", link["schedule_id"]).execute()

    db.table("executions").update({"status": "stopped"}) \
        .eq("id", execution_id).execute()
    return {"ok": True}


# ── Delete (מוחק גם schedules + תרחישים משוכפלים) ────────────────────────

@router.delete("/{execution_id}")
async def delete_execution(execution_id: str,
                           db: Client = Depends(get_supabase)):
    links_res = db.rpc("exec_links", {"p_execution_id": execution_id}).execute()
    links = links_res.data or []

    for link in links:
        if link.get("schedule_id"):
            db.table("schedules").delete().eq("id", link["schedule_id"]).execute()

    # execution_links נמחקים ב-CASCADE
    db.table("executions").delete().eq("id", execution_id).execute()

    for link in links:
        db.table("scenarios").delete().eq("id", link["scenario_id"]).execute()

    return {"ok": True}
