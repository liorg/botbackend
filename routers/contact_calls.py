# contact_calls.py — שיחות (calls) של איש קשר + flow של שיחה בודדת
# רישום ב-main.py:  app.include_router(contact_calls.router)  (כמו שאר הראוטרים)
# דורש את ה-RPC: get_call_flow (ראה get_call_flow.sql)
# חדש: paging קלאסי — גודל עמוד נקרא מ-bot_config, מפתח 'contact_call.paging'
from fastapi import APIRouter, Depends, HTTPException, Query
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/contact_calls/{contact_id}", tags=["contact-calls"])

_CALL_SELECT = (
    "id, phone_id, contact_id, scenario_id, status, started_at, ended_at, "
    "created_at, expected_end, duration_seconds, source, call_type, priority, "
    "sender_count, expected_count, mismatch_count, last_step_id, "
    "scenarios(id, name, event_type, priority, inter_leaf_response_time, estimated_duration_minutes)"
)

_PAGING_KEY     = "contact_call.paging"
_DEFAULT_PAGE   = 10
_MAX_PAGE_SIZE  = 100


def _get_page_size(db: Client) -> int:
    """גודל עמוד מ-bot_config; ברירת מחדל 10, clamp ל-1..100."""
    try:
        res = (
            db.table("bot_config")
            .select("value")
            .eq("key", _PAGING_KEY)
            .limit(1)
            .execute()
        )
        raw = (res.data or [{}])[0].get("value")
        size = int(raw)
        return max(1, min(size, _MAX_PAGE_SIZE))
    except Exception:
        return _DEFAULT_PAGE


# ── רשימת שיחות של איש קשר (ישן — ללא שינוי, backwards compat) ─────────────
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


# ── חדש: רשימת שיחות עם paging ─────────────────────────────────────────────
@router.get("/paged")
async def list_contact_calls_paged(
    contact_id: str,
    page: int = Query(1, ge=1),
    db: Client = Depends(get_supabase),
):
    """
    Paging קלאסי (offset). גודל עמוד מ-bot_config['contact_call.paging'].
    מחזיר: { calls, page, page_size, total, total_pages }
    """
    page_size = _get_page_size(db)
    start = (page - 1) * page_size
    end   = start + page_size - 1  # range כולל את שני הקצוות

    result = (
        db.table("calls")
        .select(_CALL_SELECT, count="exact")
        .eq("contact_id", contact_id)
        .order("created_at", desc=True)
        .range(start, end)
        .execute()
    )

    total       = result.count or 0
    total_pages = max(1, -(-total // page_size))  # ceil

    return {
        "calls":       result.data or [],
        "page":        page,
        "page_size":   page_size,
        "total":       total,
        "total_pages": total_pages,
    }


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
