# contact_calls.py — שיחות (calls) של איש קשר + flow של שיחה בודדת
# רישום ב-main.py:  app.include_router(contact_calls.router)  (כמו שאר הראוטרים)
# דורש את ה-RPC: get_call_flow (ראה get_call_flow.sql)
from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_supabase
from supabase import Client
from typing import Optional

router = APIRouter(prefix="/contact_calls/{contact_id}", tags=["contact-calls"])

def _unwrap(data, fn_name: str):
    """PostgREST עוטף פונקציות jsonb ברשימה, ולעיתים גם במפתח בשם הפונקציה."""
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict) and fn_name in data:
        data = data[fn_name]
    return data


_CALL_SELECT = (
    "id, phone_id, contact_id, scenario_id, status, started_at, ended_at, "
    "created_at, expected_end, duration_seconds, source, call_type, priority, "
    "sender_count, expected_count, mismatch_count, last_step_id, "
    "scenarios(id, name, event_type, priority, inter_leaf_response_time, estimated_duration_minutes)"
)


# ── רשימת שיחות של איש קשר (עם דפדוף — הכל בתוך ה-RPC) ────────────────────
@router.get("")   # בלי סלאש בסוף
@router.get("/")  # עם סלאש — שניהם עובדים, בלי תלות ב-redirect של ה-proxy
async def list_contact_calls(
    contact_id: str,
    page: int = 1,
    status: Optional[str] = None,
    page_size: Optional[int] = None,   # None ⇒ נלקח מ-bot_config
    db: Client = Depends(get_supabase),
):
    result = db.rpc(
        "rpc_list_contact_calls",
        {
            "p_contact_id": contact_id,
            "p_page":       page,
            "p_status":     status,
            "p_page_size":  page_size,
        },
    ).execute()
    return _unwrap(result.data, "rpc_list_contact_calls") or {
        "calls": [], "page": page, "page_size": 0, "total": 0, "total_pages": 1, "statuses": []
    }


# ── Flow של שיחה — RPC אחד ─────────────────────────────────────────────────
@router.get("/{call_id}/flow")
async def get_call_flow(contact_id: str, call_id: str, db: Client = Depends(get_supabase)):
    result = db.rpc(
        "get_call_flow",
        {"p_contact_id": contact_id, "p_call_id": call_id},
    ).execute()

    data = _unwrap(result.data, "get_call_flow")
    if not data or not data.get("call"):
        raise HTTPException(status_code=404, detail="Call not found")
    return data
