# scenarios_router.py
from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_supabase
from supabase import Client
from pydantic import BaseModel
from typing import Optional, Any, Literal
import uuid

router = APIRouter(prefix="/phones/{phone_id}/scenarios", tags=["scenarios"])


# ── Schemas ────────────────────────────────────────────────────────────────

class ScenarioCreate(BaseModel):
    contact_id: Optional[str] = None
    name: str
    status: Optional[str] = "draft"
    config: Optional[dict] = {}
    estimated_duration_minutes: Optional[str] = None
    inter_leaf_response_time: Optional[str] = None
    # ── Designer fields ────────────────────────────────────────────────────
    canvas: Optional[list[dict[str, Any]]] = None
    arrow_data: Optional[dict[str, Any]] = None
    interval: Optional[dict[str, Any]] = None
    estimated_time: Optional[dict[str, Any]] = None
    use_auto_calc: Optional[bool] = True
    description: Optional[str] = None
    bot_contact: Optional[dict[str, Any]] = None
    event_type: Optional[Literal["trigger", "scheduler"]] = "scheduler"   # ← default: scheduler


class ScenarioUpdate(BaseModel):
    contact_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    config: Optional[dict] = None
    estimated_duration_minutes: Optional[str] = None
    inter_leaf_response_time: Optional[str] = None
    # ── Designer fields ────────────────────────────────────────────────────
    canvas: Optional[list[dict[str, Any]]] = None
    arrow_data: Optional[dict[str, Any]] = None
    interval: Optional[dict[str, Any]] = None
    estimated_time: Optional[dict[str, Any]] = None
    use_auto_calc: Optional[bool] = None
    description: Optional[str] = None
    bot_contact: Optional[dict[str, Any]] = None
    event_type: Optional[Literal["trigger", "scheduler"]] = None


def _merge_config(existing_config: dict, body) -> dict:
    cfg = dict(existing_config or {})
    if body.canvas         is not None: cfg["canvas"]         = body.canvas
    if body.arrow_data     is not None: cfg["arrow_data"]     = body.arrow_data
    if body.interval       is not None: cfg["interval"]       = body.interval
    if body.estimated_time is not None: cfg["estimated_time"] = body.estimated_time
    if body.use_auto_calc  is not None: cfg["use_auto_calc"]  = body.use_auto_calc
    if body.description    is not None: cfg["description"]    = body.description
    if body.bot_contact    is not None: cfg["bot_contact"]    = body.bot_contact
    if body.config:
        cfg.update(body.config)
    return cfg


def _expand_config(row: dict) -> dict:
    cfg = row.get("config") or {}
    row["canvas"]         = cfg.get("canvas", [])
    row["arrow_data"]     = cfg.get("arrow_data", {})
    row["interval"]       = cfg.get("interval", {"mins": 0, "secs": 1})
    row["estimated_time"] = cfg.get("estimated_time")
    row["use_auto_calc"]  = cfg.get("use_auto_calc", True)
    row["description"]    = cfg.get("description", "")
    row["bot_contact"]    = cfg.get("bot_contact")
    # event_type — קרא מעמודה נפרדת, fallback ל-config לתאימות אחורה
    row["event_type"]     = row.get("event_type") or cfg.get("event_type", "scheduler")
    return row


# ── List scenarios ─────────────────────────────────────────────────────────
@router.get("/")
async def list_scenarios(phone_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("scenarios")
        .select(
            "id, phone_id, contact_id, name, status, config, event_type, "
            "created_at, estimated_duration_minutes, inter_leaf_response_time, "
            "contacts(id, name, number, avatar, is_bot)"
        )
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [_expand_config(r) for r in (result.data or [])]


# ── List by event_type ─────────────────────────────────────────────────────
@router.get("/by-type/{event_type}")
async def list_scenarios_by_type(
    phone_id: str,
    event_type: Literal["trigger", "scheduler"],
    db: Client = Depends(get_supabase)
):
    result = (
        db.table("scenarios")
        .select(
            "id, phone_id, contact_id, name, status, config, event_type, "
            "created_at, estimated_duration_minutes, inter_leaf_response_time, "
            "contacts(id, name, number, avatar, is_bot)"
        )
        .eq("phone_id", phone_id)
        .eq("event_type", event_type)
        .eq("status", "active")
        .order("created_at", desc=True)
        .execute()
    )
    return [_expand_config(r) for r in (result.data or [])]


# ── Get one ────────────────────────────────────────────────────────────────
@router.get("/{scenario_id}")
async def get_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("scenarios")
        .select(
            "id, phone_id, contact_id, name, status, config, event_type, "
            "created_at, estimated_duration_minutes, inter_leaf_response_time, "
            "contacts(id, name, number, avatar, is_bot)"
        )
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _expand_config(result.data)


# ── Create ─────────────────────────────────────────────────────────────────
@router.post("/")
async def create_scenario(
    phone_id: str, body: ScenarioCreate, db: Client = Depends(get_supabase)
):
    config = _merge_config({}, body)

    payload = {
        "id":         str(uuid.uuid4()),
        "phone_id":   phone_id,
        "name":       body.name,
        "status":     body.status or "draft",
        "config":     config,
        "event_type": body.event_type or "scheduler",
    }
    if body.contact_id:                 payload["contact_id"]                 = body.contact_id
    if body.estimated_duration_minutes: payload["estimated_duration_minutes"] = body.estimated_duration_minutes
    if body.inter_leaf_response_time:   payload["inter_leaf_response_time"]   = body.inter_leaf_response_time

    result = db.table("scenarios").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create scenario")
    return _expand_config(result.data[0])


# ── Update ─────────────────────────────────────────────────────────────────
@router.put("/{scenario_id}")
async def update_scenario(
    phone_id: str, scenario_id: str, body: ScenarioUpdate,
    db: Client = Depends(get_supabase)
):
    existing = (
        db.table("scenarios")
        .select("config")
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .single()
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    print(f"🔥 event_type received: {body.event_type}")  # ← הוסף
    config = _merge_config(existing.data.get("config") or {}, body)

    payload: dict = {"config": config}
    if body.name       is not None: payload["name"]       = body.name
    if body.status     is not None: payload["status"]     = body.status
    if body.contact_id is not None: payload["contact_id"] = body.contact_id
    if body.event_type is not None: payload["event_type"] = body.event_type
    if body.estimated_duration_minutes is not None:
        payload["estimated_duration_minutes"] = body.estimated_duration_minutes
    if body.inter_leaf_response_time is not None:
        payload["inter_leaf_response_time"] = body.inter_leaf_response_time
        
    result = (
        db.table("scenarios")
        .update(payload)
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _expand_config(result.data[0])


# ── Publish ────────────────────────────────────────────────────────────────
@router.post("/{scenario_id}/publish")
async def publish_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("scenarios")
        .update({"status": "active"})
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _expand_config(result.data[0])


# ── Delete ─────────────────────────────────────────────────────────────────
@router.delete("/{scenario_id}")
async def delete_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    db.table("scenarios").delete().eq("id", scenario_id).eq("phone_id", phone_id).execute()
    return {"ok": True}
