# contact_calls.py — שיחות (calls) של איש קשר + flow של שיחה בודדת
# רישום ב-main.py:  app.include_router(contact_calls.router)  (כמו שאר הראוטרים)
# דורש את ה-RPC: get_call_flow (ראה get_call_flow.sql)
from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/contact_calls/{contact_id}", tags=["contact-calls"])

_CALL_SELECT = (
    "id, phone_id, contact_id, scenario_id, status, started_at, ended_at, "
    "created_at, expected_end, duration_seconds, source, call_type, priority, "
    "sender_count, expected_count, mismatch_count, last_step_id, "
    "scenarios(id, name, event_type, priority, inter_leaf_response_time, estimated_duration_minutes)"
)


# ── רשימת שיחות של איש קשר ─────────────────────────────────────────────────
@router.get("/")
async def list_contact_calls(contact_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("calls")
        .select(_CALL_SELECT)
        .eq("contact_id", contact_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ── Flow של שיחה — RPC אחד ─────────────────────────────────────────────────
@router.get("/{call_id}/flow")
async def get_call_flow(contact_id: str, call_id: str, db: Client = Depends(get_supabase)):
    result = db.rpc(
        "get_call_flow",
        {"p_contact_id": contact_id, "p_call_id": call_id},
    ).execute()

    data = result.data
    if not data or not data.get("call"):
        raise HTTPException(status_code=404, detail="Call not found")
    return data
