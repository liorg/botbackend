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
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_current_user, get_supabase
from routers.common_schemas import Passthrough

router = APIRouter(prefix="/executions", tags=["executions"])

PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_NAME = 80


# ── Schemas ────────────────────────────────────────────────────────────────

class ExecutionTarget(BaseModel):
    phone_id: str = Field(..., description="Target phone that runs the cloned scenario.")
    contact_id: str = Field(..., description="Active contact on that phone.")


class ExecutionCreate(BaseModel):
    name: str = Field(..., description="Run name, up to 80 characters. Used in the clone and JOB names.")                       # שם הריצה
    contact_phone_number: str = Field(..., description="Contact number, 972xxxxxxxxx.")       # "972xxxxxxxxx"
    source_scenario_id: str = Field(..., description="Scenario to clone.")
    template_prefix: str = Field(..., description="Prefix for copied templates: ^[a-z][a-z0-9_]{0,39}$.")            # a-z0-9_
    targets: List[ExecutionTarget] = Field(..., description="Target phones, each with its contact. No duplicate phones.")


class ScenariosQuery(BaseModel):
    phone_ids: List[str] = Field(..., description="Phones whose scenarios to list.")


# ── Response Schemas ───────────────────────────────────────────────────────
# Every endpoint returns an exec_* RPC result. Declared fields are the ones the
# router itself relies on; everything else the RPC returns is kept.

class ExecutionListItem(Passthrough):
    """exec_list row."""


class SourcePhoneItem(Passthrough):
    """exec_list_sources row."""


class ContactSearchItem(Passthrough):
    """exec_search_contacts row."""


class SeekContactResponse(Passthrough):
    """exec_seek_contact result."""
    phones: list[Any] = Field(..., description="Phones the number is active on, with contact id, phone status and provider.")


class ExecutionScenarioItem(Passthrough):
    """exec_list_scenarios row."""


class ScenarioTemplateItem(Passthrough):
    """exec_scenario_templates row."""


class ExecutionCreateResponse(Passthrough):
    """exec_create result."""


class ExecutionRunResponse(Passthrough):
    """exec_run result."""
    execution_id: str = Field(..., description="Execution id.")


class ExecutionStopResponse(Passthrough):
    """exec_stop result."""


class ExecutionDeleteResponse(Passthrough):
    """exec_delete result; kept lists rows still referenced elsewhere."""


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


def _user_id(user: dict) -> str:
    uid = user.get("uid") if user.get("uid") is not None else user.get("id")
    if not uid:
        raise HTTPException(401, "user id missing in token")
    return uid


# ── List ───────────────────────────────────────────────────────────────────

@router.get("/",
    response_model=list[ExecutionListItem],
    summary="List executions",
    description=(
        "Returns all parallel executions, newest first. Each row includes the contact, the "
        "run/JOB name, target phone numbers, source scenario and phone, cloned template count and "
        "prefix, the number of schedules still pending or firing, and the latest schedule run "
        "time."
    ),
)
def list_executions(db: Client = Depends(get_supabase)):
    return _as_list(_rpc(db, "exec_list", {}))


# ── Source phones (Wizard שלב 3) ──────────────────────────────────────────

@router.get("/sources",
    response_model=list[SourcePhoneItem],
    summary="List source phones",
    description=(
        "Returns phones that own at least one active original scenario (clones excluded), with "
        "the number of such scenarios. Used to choose the source phone in wizard step 3."
    ),
)
def list_sources(db: Client = Depends(get_supabase)):
    return _as_list(_rpc(db, "exec_list_sources", {}))


# ── Autocomplete — אנשי קשר active של המשתמש (Wizard שלב 1) ──────────────

@router.get("/contacts",
    response_model=list[ContactSearchItem],
    summary="Search active contacts (autocomplete)",
    description=(
        "Autocomplete for wizard step 1. Searches the current user's active contacts across their "
        "phones, grouped by contact number. Digits match the number, text matches the name, and "
        "an empty query returns the contacts linked to the most phones first. Returns number, "
        "name, phone count and phone numbers."
    ),
)
def search_contacts(
    q: str = Query("", max_length=64),
    limit: int = Query(20, ge=1, le=50),
    db: Client = Depends(get_supabase),
    user: dict = Depends(get_current_user),
):
    return _as_list(_rpc(db, "exec_search_contacts", {
        "p_user_id": _user_id(user),
        "p_q": q.strip(),
        "p_limit": limit,
    }))


# ── Seek contact (Wizard שלב 1→2) ─────────────────────────────────────────

@router.get("/seek/{phone_number}",
    response_model=SeekContactResponse,
    summary="Find an active contact by number",
    description=(
        "Looks up a contact number (972xxxxxxxxx) and returns every phone it is active on, with "
        "the contact id, phone status and provider for each. Responds 404 when the number is not "
        "active on any phone."
    ),
)
def seek_contact(phone_number: str, db: Client = Depends(get_supabase)):
    pn = _validate_phone_number(phone_number)
    data = _rpc(db, "exec_seek_contact", {"p_phone_number": pn})
    data = data if data is not None else {}
    if not data.get("phones"):
        raise HTTPException(404, f"No active contact found for {pn}")
    return data


# ── Scenarios of a phone (Wizard שלב 3) ───────────────────────────────────

@router.post("/scenarios",
    response_model=list[ExecutionScenarioItem],
    summary="List scenarios of phones",
    description=(
        "Returns the active original scenarios (clones excluded) of the given phone ids, with "
        "name, event type and description."
    ),
)
def scenarios_for_phones(body: ScenariosQuery, db: Client = Depends(get_supabase)):
    if not body.phone_ids:
        raise HTTPException(400, "phone_ids is required")
    return _as_list(_rpc(db, "exec_list_scenarios", {"p_phone_ids": body.phone_ids}))


# ── Templates used by a scenario (Wizard שלב 3/4 — תצוגה מקדימה) ─────────

@router.get("/scenario-templates/{scenario_id}",
    response_model=list[ScenarioTemplateItem],
    summary="List templates used by a scenario",
    description=(
        "Returns the source phone's templates referenced anywhere in the scenario config (by "
        "templateId, or by templateName and templateLang). These are the templates that will be "
        "cloned to each target phone."
    ),
)
def scenario_templates(scenario_id: str, db: Client = Depends(get_supabase)):
    return _as_list(_rpc(db, "exec_scenario_templates", {"p_scenario_id": scenario_id}))


# ── Create ─────────────────────────────────────────────────────────────────

@router.post("/",
    response_model=ExecutionCreateResponse,
    summary="Create an execution",
    description=(
        "Atomically creates an execution and, for each target phone: copies every template the "
        "source scenario uses as \"{prefix}_{template}\" (approved and published, Baileys only), "
        "clones the scenario as \"{run} {scenario}\" with template references remapped to the "
        "copies, and creates a manual schedule named \"{run} JOB\" that does not fire until the "
        "execution is run. Responds 409 if an execution already exists for the contact number."
    ),
)
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

@router.post("/{execution_id}/run",
    response_model=ExecutionRunResponse,
    summary="Run an execution",
    description=(
        "Sets next_run=now on all of the execution's schedules so the Scheduler fires them in "
        "parallel on its next poll. The backend never calls the Spine. Responds 409 while any "
        "schedule is still firing."
    ),
)
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

@router.post("/{execution_id}/stop",
    response_model=ExecutionStopResponse,
    summary="Stop an execution",
    description=(
        "Pauses the execution's schedules that have not fired yet (next_run cleared) and marks "
        "the execution stopped. Schedules already firing are not interrupted."
    ),
)
def stop_execution(execution_id: str, db: Client = Depends(get_supabase)):
    return _rpc(db, "exec_stop", {"p_execution_id": execution_id})


# ── Delete — תזמונים + תרחישים + תבניות משוכפלים ─────────────────────────

@router.delete("/{execution_id}",
    response_model=ExecutionDeleteResponse,
    summary="Delete an execution",
    description=(
        "Deletes the execution and its cloned schedules, scenarios and templates. Rows still "
        "referenced by other records (for example calls that already ran) are kept, with "
        "schedules paused, and listed in \"kept\". Responds 409 while any schedule is firing."
    ),
)
def delete_execution(execution_id: str, db: Client = Depends(get_supabase)):
    data = _rpc(db, "exec_delete", {"p_execution_id": execution_id})
    data = data if data is not None else {}
    err = data.get("error")
    if err == "not_found":
        raise HTTPException(404, "Execution not found")
    if err == "firing":
        raise HTTPException(409, "Execution is firing — try again shortly")
    return data
