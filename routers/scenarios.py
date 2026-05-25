from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_supabase
from supabase import Client
from pydantic import BaseModel
from typing import Optional
import uuid

# ── Router mounted at /phones/{phone_id}/scenarios ─────────────────────────
# Include in main.py as:
#   from routers import scenarios
#   app.include_router(scenarios.router)

router = APIRouter(prefix="/phones/{phone_id}/scenarios", tags=["scenarios"])


# ── Schemas ────────────────────────────────────────────────────────────────

class ScenarioCreate(BaseModel):
    contact_id: Optional[str] = None          # bot contact
    name: str
    status: Optional[str] = "draft"
    config: Optional[dict] = {}
    estimated_duration_minutes: Optional[str] = None   # "00:05:00"
    inter_leaf_response_time: Optional[str] = None     # "00:00:10"


class ScenarioUpdate(BaseModel):
    contact_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    config: Optional[dict] = None
    estimated_duration_minutes: Optional[str] = None
    inter_leaf_response_time: Optional[str] = None


# ── List ───────────────────────────────────────────────────────────────────
@router.get("/")
async def list_scenarios(phone_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("scenarios")
        .select(
            "id, phone_id, contact_id, name, status, config, "
            "created_at, estimated_duration_minutes, inter_leaf_response_time, "
            "contacts(id, name, number, avatar, is_bot)"
        )
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ── Get one ────────────────────────────────────────────────────────────────
@router.get("/{scenario_id}")
async def get_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("scenarios")
        .select(
            "id, phone_id, contact_id, name, status, config, "
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
    return result.data


# ── Create ─────────────────────────────────────────────────────────────────
@router.post("/")
async def create_scenario(
    phone_id: str, body: ScenarioCreate, db: Client = Depends(get_supabase)
):
    payload = {
        "id": str(uuid.uuid4()),
        "phone_id": phone_id,
        "name": body.name,
        "status": body.status or "draft",
        "config": body.config or {},
    }
    if body.contact_id:
        payload["contact_id"] = body.contact_id
    if body.estimated_duration_minutes:
        payload["estimated_duration_minutes"] = body.estimated_duration_minutes
    if body.inter_leaf_response_time:
        payload["inter_leaf_response_time"] = body.inter_leaf_response_time

    result = db.table("scenarios").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create scenario")
    return result.data[0]


# ── Update (PUT — full replace of mutable fields) ─────────────────────────
@router.put("/{scenario_id}")
async def update_scenario(
    phone_id: str, scenario_id: str, body: ScenarioUpdate,
    db: Client = Depends(get_supabase)
):
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="No fields to update")
    result = (
        db.table("scenarios")
        .update(payload)
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return result.data[0]


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
    return result.data[0]


# ── Delete ─────────────────────────────────────────────────────────────────
@router.delete("/{scenario_id}")
async def delete_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    db.table("scenarios").delete().eq("id", scenario_id).eq("phone_id", phone_id).execute()
    return {"ok": True}