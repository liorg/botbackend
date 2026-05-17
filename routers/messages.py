# messages.py  –  FastAPI router
# GET /messages/contact/{contact_id}  →  כל ההודעות של contact

from fastapi import APIRouter, Depends, Query
from dependencies import get_supabase
from supabase import Client
import json, re
from datetime import datetime

router = APIRouter(prefix="/messages", tags=["messages"])


def parse_content(raw):
    """content מגיע כ-JSON string או כ-dict"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {"text": raw, "type": "text"}
    return raw or {}


def date_label(s):
    if not s:
        return ""
    try:
        clean = re.sub(r"\.\d+", "", str(s)).replace("Z", "").replace("T", " ")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ""


def fmt_dt(s):
    if not s:
        return ""
    try:
        clean = re.sub(r"\.\d+", "", str(s)).replace("Z", "").replace("T", " ")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(s)


def format_message(msg, phone_number: str = ""):
    content = parse_content(msg.get("content"))
    ts = msg.get("sent_at") or msg.get("created_at") or ""
    sender = msg.get("sender", "")

    # קביעת כיוון: אם ה-sender הוא מספר הטלפון של הבוט → יוצא, אחרת נכנס
    is_bot = sender == phone_number if phone_number else False

    return {
        "id":        msg["id"],
        "contact_id": msg.get("contact_id"),
        "from":      "bot" if is_bot else "user",
        "sender":    sender,
        "type":      content.get("type", "text"),
        "text":      content.get("text") or content.get("body") or "",
        "timestamp": str(ts),
        "date":      date_label(str(ts)),
        "buttons":   content.get("buttons"),
        "imageUrl":  content.get("imageUrl"),
        "audioUrl":  content.get("audioUrl"),
        "fileName":  content.get("fileName"),
    }


# ── GET /messages/contact/{contact_id} ────────────────────────────────────────
@router.get("/contact/{contact_id}")
async def get_contact_messages(
    contact_id: str,
    limit: int = Query(200, le=500),
    phone_number: str = Query("", description="מספר הטלפון של הבוט לזיהוי כיוון"),
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("messages")
        .select("id, contact_id, sender, content, sent_at, created_at")
        .eq("contact_id", contact_id)
        .order("sent_at")
        .limit(limit)
        .execute()
    )
    msgs = result.data or []
    return [format_message(m, phone_number) for m in msgs]