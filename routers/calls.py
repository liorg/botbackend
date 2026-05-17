# calls.py  –  FastAPI router (גרסה מעודכנת)
# שינוי עיקרי: GET /calls/phone/{phone_id} מחזיר כל call עם messages מוטבעות

from fastapi import APIRouter, Depends
from dependencies import get_supabase, get_current_user
from supabase import Client

router = APIRouter(prefix="/calls", tags=["calls"])


# ─────────────────────────────────────────────────────────────────────────────
# GET /calls/phone/{phone_id}
# מחזיר את כל ה-calls של הטלפון, כל call עם ה-messages שלו
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/phone/{phone_id}")
async def list_calls(
    phone_id: str,
    db: Client = Depends(get_supabase),
    user=Depends(get_current_user),
):
    # ── 1. שלוף calls ──────────────────────────────────────────────────────
    calls_res = (
        db.table("calls")
        .select("id, status, created_at, ended_at, contact_id, scenario_id, contacts(name, number), scenarios(name)")
        .eq("phone_id", phone_id)
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    calls = calls_res.data or []

    if not calls:
        return []

    # ── 2. שלוף כל ה-messages בבת אחת (batch) ──────────────────────────────
    call_ids = [c["id"] for c in calls]

    msgs_res = (
        db.table("messages")
        .select("id, call_id, sender, content, sent_at, created_at")
        .in_("call_id", call_ids)
        .order("sent_at")
        .execute()
    )

    # group by call_id
    msgs_by_call: dict[str, list] = {}
    for msg in (msgs_res.data or []):
        msgs_by_call.setdefault(msg["call_id"], []).append(msg)

    # ── 3. הרכב תשובה ─────────────────────────────────────────────────────
    return [
        {
            "id":        c["id"],
            "status":    c["status"] or "stuck",
            "created_at": c["created_at"],
            "ended_at":   c.get("ended_at"),
            "contact":   (c.get("contacts") or {}).get("name") or (c.get("contacts") or {}).get("number") or "",
            "scenario":  (c.get("scenarios") or {}).get("name") or "",
            "messages":  msgs_by_call.get(c["id"], []),
        }
        for c in calls
    ]


# ─────────────────────────────────────────────────────────────────────────────
# POST /calls/
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/")
async def create_call(body: dict, db: Client = Depends(get_supabase), user=Depends(get_current_user)):
    result = db.table("calls").insert({**body, "user_id": user.id}).execute()
    return result.data[0]


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /calls/{call_id}
# ─────────────────────────────────────────────────────────────────────────────
@router.patch("/{call_id}")
async def update_call(call_id: str, body: dict, db: Client = Depends(get_supabase), user=Depends(get_current_user)):
    result = db.table("calls").update(body).eq("id", call_id).eq("user_id", user.id).execute()
    return result.data[0]


# ─────────────────────────────────────────────────────────────────────────────
# GET /calls/{call_id}/messages
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/{call_id}/messages")
async def list_messages(call_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("messages")
        .select("*")
        .eq("call_id", call_id)
        .order("sent_at")
        .execute()
    )
    return result.data


# ─────────────────────────────────────────────────────────────────────────────
# POST /calls/{call_id}/messages
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/{call_id}/messages")
async def add_message(call_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("messages").insert({
        "call_id": call_id,
        "sender":  body["sender"],
        "content": body["content"],
    }).execute()
    return result.data[0]