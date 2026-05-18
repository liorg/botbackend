from fastapi import APIRouter, Depends, Query
from dependencies import get_supabase
from supabase import Client
import json, re
from datetime import datetime

router = APIRouter(prefix="/messages", tags=["messages"])


def parse_content(raw):
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


def format_message(msg, phone_number: str = ""):
    content  = parse_content(msg.get("content"))
    ts       = msg.get("sent_at") or ""
    sender   = msg.get("sender", "")
    raw_type = content.get("type", "text")

    # direction: true = יוצא (בוט), false = נכנס (משתמש)
    # אם direction קיים — משתמשים בו, אחרת מסתמכים על phone_number
    direction = msg.get("direction")
    if direction is True:
        is_bot = True
    elif direction is False:
        is_bot = False
    else:
        is_bot = bool(phone_number and sender == phone_number)

    # ── נרמול type ────────────────────────────────────────────────────────────
    if raw_type == "list_message":
        msg_type = "menu"
    elif raw_type == "image":
        msg_type = "image"
    elif raw_type == "audio":
        msg_type = "audio"
    elif raw_type == "document":
        msg_type = "file"
    elif raw_type in ("buttons", "button"):
        msg_type = "buttons"
    elif raw_type in ("button_reply", "buttonsResponseMessage"):
        msg_type = "button_reply"
    else:
        msg_type = "text"

    # ── טקסט ─────────────────────────────────────────────────────────────────
    text = (
        content.get("text")
        or content.get("body")
        or content.get("caption")   # image caption
        or content.get("description")  # list_message header
        or ""
    )

    # ── list_message → options ────────────────────────────────────────────────
    options = None
    menu_button_label = None
    menu_title = None
    if msg_type == "menu":
        menu_button_label = content.get("buttonText") or "לחצו לבחירה"
        menu_title        = content.get("title") or "בחרו נושא"
        sections          = content.get("sections") or []
        options = []
        for sec in sections:
            for row in (sec.get("rows") or []):
                options.append({
                    "title":    row.get("title", ""),
                    "subtitle": row.get("description") or None,
                    "rowId":    row.get("rowId"),
                })

    # ── buttons ───────────────────────────────────────────────────────────────
    buttons = None
    if msg_type == "buttons":
        raw_btns = content.get("buttons") or []
        buttons  = [{"label": b.get("text") or b.get("label") or b.get("title", "")} for b in raw_btns]

    return {
        "id":               msg["id"],
        "contact_id":       msg.get("contact_id"),
        "from":             "bot" if is_bot else "user",
        "sender":           sender,
        "type":             msg_type,
        "text":             text,
        "timestamp":        str(ts),
        "date":             date_label(str(ts)),
        "buttons":          buttons,
        "options":          options,
        "menuButtonLabel":  menu_button_label,
        "menuTitle":        menu_title,
        "imageUrl":         content.get("imageUrl") or content.get("url") or content.get("image"),
        "audioUrl":         content.get("audioUrl") or content.get("url"),
        "fileName":         content.get("fileName") or content.get("filename"),
    }


@router.get("/contact/{contact_id}")
async def get_contact_messages(
    contact_id: str,
    limit: int = Query(200, le=500),
    phone_number: str = Query("", description="מספר הטלפון של הבוט לזיהוי כיוון"),
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("messages")
        .select("id, contact_id, sender, content, sent_at, direction")
        .eq("contact_id", contact_id)
        .order("sent_at")
        .limit(limit)
        .execute()
    )
    msgs = result.data or []
    return [format_message(m, phone_number) for m in msgs]
