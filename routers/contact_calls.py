# contact_calls.py — שיחות (calls) של איש קשר + flow של שיחה בודדת
# רישום ב-main.py:  app.include_router(contact_calls.router)  (כמו שאר הראוטרים)
# דורש את ה-RPC: get_call_flow (ראה get_call_flow.sql)
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from dependencies import get_supabase
from supabase import Client
from typing import Any, Optional

from routers.common_schemas import Passthrough

router = APIRouter(prefix="/contact_calls/{contact_id}", tags=["contact-calls"])

# ── Response Schemas ───────────────────────────────────────────────────────
# The shapes come from the RPCs (rpc_list_contact_calls / get_call_flow);
# fields that are not declared are kept as-is.

class ContactCallItem(Passthrough):
    id: Optional[str] = Field(None, description="Call id.")
    status: Optional[str] = Field(None, description="Call status.")


class ContactCallsPage(Passthrough):
    calls: list[ContactCallItem] = Field([], description="Calls on this page.")
    page: int = Field(1, description="Current page, starting at 1.")
    page_size: int = Field(0, description="Rows per page (from bot_config when not sent).")
    total: int = Field(0, description="Total calls matching the filter.")
    total_pages: int = Field(1, description="Number of pages.")
    statuses: list[Any] = Field([], description="Statuses that exist for this contact, for the filter.")


class CallFlowResponse(Passthrough):
    call: dict[str, Any] = Field(..., description="The call with its flow, as returned by get_call_flow.")


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
@router.get("", response_model=ContactCallsPage, summary="List contact calls (paged)", description="Returns the contact's calls with paging and an optional status filter. Page size comes from bot_config when page_size is not sent.")   # בלי סלאש בסוף
@router.get("/", response_model=ContactCallsPage, summary="List contact calls (paged)", description="Returns the contact's calls with paging and an optional status filter. Page size comes from bot_config when page_size is not sent.")  # עם סלאש — שניהם עובדים, בלי תלות ב-redirect של ה-proxy
def list_contact_calls(
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
@router.get("/{call_id}/flow", response_model=CallFlowResponse, summary="Get call flow", description="Returns one call of the contact with its flow. Returns 404 when the call does not exist.")
def get_call_flow(contact_id: str, call_id: str, db: Client = Depends(get_supabase)):
    result = db.rpc(
        "get_call_flow",
        {"p_contact_id": contact_id, "p_call_id": call_id},
    ).execute()

    data = _unwrap(result.data, "get_call_flow")
    if not data or not data.get("call"):
        raise HTTPException(status_code=404, detail="Call not found")
    return data
