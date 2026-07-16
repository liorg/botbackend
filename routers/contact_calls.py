# contact_calls.py — שיחות (calls) של איש קשר + flow של שיחה בודדת
# רישום ב-main.py:  app.include_router(contact_calls.router)  (כמו שאר הראוטרים)
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


# ── Flow של שיחה: snapshot + עלים + נתוני retry ────────────────────────────
@router.get("/{call_id}/flow")
async def get_call_flow(contact_id: str, call_id: str, db: Client = Depends(get_supabase)):
    call_res = (
        db.table("calls")
        .select(_CALL_SELECT + ", scenario_snapshot, variables")
        .eq("id", call_id)
        .eq("contact_id", contact_id)
        .single()
        .execute()
    )
    if not call_res.data:
        raise HTTPException(status_code=404, detail="Call not found")
    call = call_res.data

    # עלים שבוצעו בפועל
    leaves_res = (
        db.table("spine_leaves")
        .select("leaf_id, step_id, type, content, wa_type, status, timestamp, meta")
        .eq("call_id", call_id)
        .order("timestamp")
        .execute()
    )
    leaves = leaves_res.data or []

    # הודעות של השיחה — retry_counter + whatsapp_message_id + סטטוס מסירה
    msgs_res = (
        db.table("messages")
        .select("id, leaf_id, retry_counter, whatsapp_message_id, status, sent_at, direction")
        .eq("call_id", call_id)
        .execute()
    )
    msg_by_leaf: dict = {}
    for m in (msgs_res.data or []):
        lid = m.get("leaf_id")
        if not lid:
            continue
        prev = msg_by_leaf.get(lid)
        # שומרים את ההודעה עם ה-retry הגבוה ביותר (הניסיון האחרון)
        if not prev or (m.get("retry_counter") or 1) >= (prev.get("retry_counter") or 1):
            msg_by_leaf[lid] = m

    for leaf in leaves:
        leaf["message"] = msg_by_leaf.get(leaf["leaf_id"])

    snapshot = call.pop("scenario_snapshot", None) or {}

    return {"call": call, "snapshot": snapshot, "leaves": leaves}
