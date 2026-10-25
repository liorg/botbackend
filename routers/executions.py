# routers/executions.py
#
# ביצועים מקבילים — תרחיש מטלפון מקור, משוכפל לכמה טלפונים מול איש קשר active אחד.
#   תרחיש:  "{run} {scenario}"
#   תבנית:  "{prefix}_{template}"   approved + published (baileys)
#   תזמון:  "{run} JOB"              manual — exec_run מציב next_run=now, ה-Scheduler יורה
#
# ה-Backend לא פונה ל-Spine. הכול דרך DB.
#
# רישום ב-main.py:
#   from routers import executions
#   app.include_router(executions.router, prefix="/api")

import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from dependencies import get_supabase

router = APIRouter(prefix="/executions", tags=["executions"])

PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_NAME = 80


# ── Schemas ────────────────────────────────────────────────────────────────

class ExecutionTarget(BaseModel):
    phone_id: str
    contact_id: str


class ExecutionCreate(BaseModel):
    name: str                       # שם הריצה
    contact_phone_number: str       # "972xxxxxxxxx"
    source_scenario_id: str
    template_prefix: str            # a-z0-9_
    targets: List[ExecutionTarget]


class ScenariosQuery(BaseModel):
    phone_ids: List[str]


# ── Helpers ────────────────────────────────────────────────────────────────

def _validate_phone_number(value: str) -> str:
    v = (value or "").strip().lstrip("+")
    if not (v.isdigit() and v.startswith("972") and 11 <= len(v) <= 13):
        raise HTTPException(400, "contact_phone_number must be like 972xxxxxxxxx")
    return v


def _db_error(exc: Exception) -> str:
    msg = getattr(exc, "message", None)
    return msg if msg else str(exc)


def _rpc(db: Client, fn: str, params: dict):
    try:
        return db.rpc(fn, params).execute().data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{fn}: {_db_error(exc)}")


def _as_list(data):
    return data if data is not None else []


# ── List ───────────────────────────────────────────────────────────────────

@router.get("/")
def list_executions(db: Client = Depends(get_supabase)):
    return _as_list(_rpc(db, "exec_list", {}))


# ── Source phones (Wizard שלב 3) ──────────────────────────────────────────

@router.get("/sources")
def list_sources(db: Client = Depends(get_supabase)):
    return _as_list(_rpc(db, "exec_list_sources", {}))


# ── Seek contact (Wizard שלב 1→2) ─────────────────────────────────────────

@router.get("/seek/{phone_number}")
def seek_contact(phone_number: str, db: Client = Depends(get_supabase)):
    pn = _validate_phone_number(phone_number)
    data = _rpc(db, "exec_seek_contact", {"p_phone_number": pn})
    data = data if data is not None else {}
    if not data.get("phones"):
        raise HTTPException(404, f"No active contact found for {pn}")
    return data


# ── Scenarios of a phone (Wizard שלב 3) ───────────────────────────────────

@router.post("/scenarios")
def scenarios_for_phones(body: ScenariosQuery, db: Client = Depends(get_supabase)):
    if not body.phone_ids:
        raise HTTPException(400, "phone_ids is required")
    return _as_list(_rpc(db, "exec_list_scenarios", {"p_phone_ids": body.phone_ids}))


# ── Templates used by a scenario (Wizard שלב 3/4 — תצוגה מקדימה) ─────────

@router.get("/scenario-templates/{scenario_id}")
def scenario_templates(scenario_id: str, db: Client = Depends(get_supabase)):
    return _as_list(_rpc(db, "exec_scenario_templates", {"p_scenario_id": scenario_id}))


# ── Create ─────────────────────────────────────────────────────────────────

@router.post("/")
def create_execution(body: ExecutionCreate, db: Client = Depends(get_supabase)):
    pn = _validate_phone_number(body.contact_phone_number)

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if len(name) > MAX_NAME:
        raise HTTPException(400, f"name is longer than {MAX_NAME}")

    prefix = (body.template_prefix or "").strip()
    if not PREFIX_RE.match(prefix):
        raise HTTPException(400, "template_prefix must match ^[a-z][a-z0-9_]{0,39}$")

    if not body.targets:
        raise HTTPException(400, "targets is required")
    phone_ids = [t.phone_id for t in body.targets]
    if len(set(phone_ids)) != len(phone_ids):
        raise HTTPException(400, "duplicate phone in targets")

    existing = (db.table("executions")
                  .select("id")
                  .eq("contact_phone_number", pn)
                  .execute())
    if existing.data:
        raise HTTPException(409, f"Execution already exists for {pn}")

    return _rpc(db, "exec_create", {
        "p_name": name,
        "p_contact_phone_number": pn,
        "p_source_scenario_id": body.source_scenario_id,
        "p_template_prefix": prefix,
        "p_targets": [t.model_dump() for t in body.targets],
    })


# ── Run — next_run=now לכל ה-JOBs, ה-Scheduler יורה במקביל ────────────────

@router.post("/{execution_id}/run")
def run_execution(execution_id: str, db: Client = Depends(get_supabase)):
    data = _rpc(db, "exec_run", {"p_execution_id": execution_id})
    data = data if data is not None else {}
    err = data.get("error")
    if err == "not_found":
        raise HTTPException(404, "Execution not found")
    if err == "no_schedules":
        raise HTTPException(404, "Execution has no schedules")
    if err == "firing":
        raise HTTPException(409, f"{data.get('firing')} schedule(s) still firing")
    return {"execution_id": execution_id, **data}


# ── Stop ───────────────────────────────────────────────────────────────────

@router.post("/{execution_id}/stop")
def stop_execution(execution_id: str, db: Client = Depends(get_supabase)):
    return _rpc(db, "exec_stop", {"p_execution_id": execution_id})


# ── Delete — תזמונים + תרחישים + תבניות משוכפלים ─────────────────────────

@router.delete("/{execution_id}")
def delete_execution(execution_id: str, db: Client = Depends(get_supabase)):
    data = _rpc(db, "exec_delete", {"p_execution_id": execution_id})
    data = data if data is not None else {}
    err = data.get("error")
    if err == "not_found":
        raise HTTPException(404, "Execution not found")
    if err == "firing":
        raise HTTPException(409, "Execution is firing — try again shortly")
    return data
