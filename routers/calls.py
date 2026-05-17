from fastapi import APIRouter, Depends, Query
from dependencies import get_supabase
from supabase import Client
import re
from datetime import datetime

router = APIRouter(prefix="/calls", tags=["calls"])


def fmt_dt(s):
    if not s:
        return ""
    try:
        clean = re.sub(r"\.\d+", "", s).replace("Z", "").replace("T", " ")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return s or ""


def date_label(s):
    if not s:
        return ""
    try:
        clean = re.sub(r"\.\d+", "", s).replace("Z", "").replace("T", " ")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ""


def format_message(msg):
    content = msg.get("content") or {}
    ts = msg.get("sent_at") or msg.get("created_at") or ""
    return {
        "id":       msg["id"],
        "call_id":  msg.get("call_id"),
        "from":     msg.get("sender", "bot"),
        "type":     content.get("type", "text"),
        "text":     content.get("text") or content.get("body") or "",
        "timestamp": ts,
        "date":     date_label(ts),
        "buttons":  content.get("buttons"),
        "imageUrl": content.get("imageUrl"),
        "audioUrl": content.get("audioUrl"),
        "fileName": content.get("fileName"),
    }


# ── GET /calls/phone/{phone_id} ───────────────────────────────────────────────
@router.get("/phone/{phone_id}")
async def list_calls(
    phone_id: str,
    limit: int = Query(100, le=300),
    db: Client = Depends(get_supabase),
):
    # 1. calls בלבד — בלי * כדי למנוע קריסת nested join
    calls_res = (
        db.table("calls")
        .select("id, status, created_at, ended_at, contact_id, scenario_id, contacts(name, number), scenarios(name)")
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    calls = calls_res.data or []
    if not calls:
        return []

    # 2. messages — batch יחיד לכל ה-calls
    call_ids = [c["id"] for c in calls]
    msgs_res = (
        db.table("messages")
        .select("id, call_id, sender, content, sent_at, created_at")
        .in_("call_id", call_ids)
        .order("sent_at")
        .execute()
    )

    msgs_by_call = {}
    for msg in (msgs_res.data or []):
        msgs_by_call.setdefault(msg["call_id"], []).append(format_message(msg))

    # 3. הרכב
    return [
        {
            "id":         c["id"],
            "status":     c.get("status") or "stuck",
            "created_at": c.get("created_at"),
            "ended_at":   c.get("ended_at"),
            "start":      fmt_dt(c.get("created_at")),
            "end":        fmt_dt(c.get("ended_at")),
            "contact":    (c.get("contacts") or {}).get("name")
                          or (c.get("contacts") or {}).get("number") or "",
            "scenario":   (c.get("scenarios") or {}).get("name") or "",
            "messages":   msgs_by_call.get(c["id"], []),
        }
        for c in calls
    ]


# ── POST /calls/ ──────────────────────────────────────────────────────────────
@router.post("/")
async def create_call(body: dict, db: Client = Depends(get_supabase)):
    result = db.table("calls").insert(body).execute()
    return result.data[0]


# ── PATCH /calls/{call_id} ────────────────────────────────────────────────────
@router.patch("/{call_id}")
async def update_call(call_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("calls").update(body).eq("id", call_id).execute()
    return result.data[0]


# ── GET /calls/{call_id}/messages ─────────────────────────────────────────────
@router.get("/{call_id}/messages")
async def list_messages(call_id: str, db: Client = Depends(get_supabase)):
    result = (
        db.table("messages")
        .select("id, call_id, sender, content, sent_at, created_at")
        .eq("call_id", call_id)
        .order("sent_at")
        .execute()
    )
    return [format_message(m) for m in (result.data or [])]


# ── POST /calls/{call_id}/messages ────────────────────────────────────────────
@router.post("/{call_id}/messages")
async def add_message(call_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("messages").insert({
        "call_id": call_id,
        "sender":  body["sender"],
        "content": body["content"],
    }).execute()
    return result.data[0]