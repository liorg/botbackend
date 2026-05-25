from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from dependencies import get_supabase, get_current_user
from supabase import Client
import json, re, os, io
import httpx
from datetime import datetime

router = APIRouter(prefix="/messages", tags=["messages"])

# ── Config ────────────────────────────────────────────────────────────
BACKEND_URL = os.getenv("BACKEND_URL", "https://vid.michal-solutions.com/api")

# ── Helpers ───────────────────────────────────────────────────────────
def build_media_url(media_url: str, phone_id: str) -> str | None:
    if not media_url or not phone_id:
        return None
    msg_id = media_url.split("/media/")[-1]
    return f"{BACKEND_URL}/messages/media/{phone_id}/{msg_id}"

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

def format_message(msg, phone_number: str = "", phone_id: str = ""):
    content  = parse_content(msg.get("content"))
    ts       = msg.get("sent_at") or ""
    sender   = msg.get("sender", "")
    raw_type = content.get("type", "text")

    # ── כיוון ────────────────────────────────────────────────────────
    direction = msg.get("direction")
    if direction is True:
        is_bot = True
    elif direction is False:
        is_bot = False
    else:
        is_bot = bool(phone_number and sender == phone_number)

    # ── נרמול type ───────────────────────────────────────────────────
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

    # ── טקסט ─────────────────────────────────────────────────────────
    text = (
        content.get("text")
        or content.get("body")
        or content.get("caption")
        or content.get("description")
        or ""
    )

    # ── media URL ─────────────────────────────────────────────────────
    raw_media      = msg.get("media_url") or content.get("mediaUrl")
    media_url_full = build_media_url(raw_media, phone_id or msg.get("phone_id", ""))

    # ── list_message → options ────────────────────────────────────────
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

    # ── buttons ───────────────────────────────────────────────────────
    buttons = None
    if msg_type == "buttons":
        raw_btns = content.get("buttons") or []
        buttons  = [{"label": b.get("text") or b.get("label") or b.get("title", "")} for b in raw_btns]

    return {
        "id":              msg["id"],
        "contact_id":      msg.get("contact_id"),
        "from":            "bot" if is_bot else "user",
        "sender":          sender,
        "type":            msg_type,
        "text":            text,
        "timestamp":       str(ts),
        "date":            date_label(str(ts)),
        "buttons":         buttons,
        "options":         options,
        "menuButtonLabel": menu_button_label,
        "menuTitle":       menu_title,
        "imageUrl":        media_url_full if msg_type == "image" else None,
        "audioUrl":        media_url_full if msg_type == "audio" else None,
        "fileName":        content.get("fileName") or content.get("filename"),
    }


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("/contact/{contact_id}")
async def get_contact_messages(
    contact_id: str,
    limit: int = Query(200, le=500),
    phone_number: str = Query(""),
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .eq("contact_id", contact_id)
        .order("sent_at")
        .limit(limit)
        .execute()
    )
    return [format_message(m, phone_number) for m in (result.data or [])]


@router.get("/phone/{phone_id}/contact/{contact_id}")
async def get_messages_by_phone_and_contact(
    phone_id: str,
    contact_id: str,
    limit: int = Query(200, le=500),
    db: Client = Depends(get_supabase),
):
    phone_res    = db.table("phones").select("number").eq("id", phone_id).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    result = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .eq("phone_id", phone_id)
        .eq("contact_id", contact_id)
        .order("sent_at")
        .limit(limit)
        .execute()
    )
    msgs = result.data or []

    if not msgs:
        fallback = (
            db.table("messages")
            .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
            .eq("contact_id", contact_id)
            .is_("phone_id", "null")
            .order("sent_at")
            .limit(limit)
            .execute()
        )
        msgs = fallback.data or []

    return [format_message(m, phone_number, phone_id) for m in msgs]


@router.get("/phone/{phone_id}")
async def get_all_phone_messages(
    phone_id: str,
    limit: int = Query(500, le=1000),
    db: Client = Depends(get_supabase),
):
    phone_res    = db.table("phones").select("number").eq("id", phone_id).limit(1).execute()
    phone_number = (phone_res.data or [{}])[0].get("number", "")

    result = (
        db.table("messages")
        .select("id, contact_id, phone_id, sender, content, sent_at, direction, media_url")
        .eq("phone_id", phone_id)
        .order("sent_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [format_message(m, phone_number, phone_id) for m in (result.data or [])]


# ── Media Proxy — מסתיר את ה-agent ───────────────────────────────────

async def _get_agent_api_port(db: Client, phone_id: str):
    """שלוף IP ו-port של ה-agent"""
    try:
        res = (
            db.table("phones")
            .select("api_port, agent_hosts(ip_address)")
            .eq("id", phone_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None, None
        phone    = res.data[0]
        api_port = phone.get("api_port")
        host     = phone.get("agent_hosts") or {}
        ip       = host.get("ip_address")
        return ip, api_port
    except Exception:
        return None, None


@router.get("/media/{phone_id}/{message_id}")
async def proxy_media(
    phone_id: str,
    message_id: str,
    db: Client = Depends(get_supabase),
):
    """Proxy תמונות/אודיו — ה-agent נסתר מהclient"""
    ip, api_port = await _get_agent_api_port(db, phone_id)
    if not ip or not api_port:
        raise HTTPException(404, "Agent not found")

    agent_url = f"http://{ip}:{api_port}/media/{message_id}"

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(agent_url)
            if r.status_code == 404:
                raise HTTPException(404, "Media not found")
            return StreamingResponse(
                io.BytesIO(r.content),
                media_type=r.headers.get("content-type", "application/octet-stream")
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Agent unavailable: {e}")