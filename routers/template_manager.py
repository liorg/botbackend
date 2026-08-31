# routers/template_manager.py
"""
TemplateManager — ניהול תבניות הודעה בסגנון WhatsApp, פר טלפון.

מבנה תבנית (עמודת content, JSONB):
    {
      "header":  { "format": "text|image|video|document|none", "text": "שלום {{1}}" },
      "body":    { "text": "התור שלך ל-{{1}} בשעה {{2}}" },
      "footer":  { "text": "מרפאת מיכל" },
      "buttons": [ { "type": "quick_reply", "text": "אישור" } ]
    }

דוגמאות (עמודת examples, JSONB) — ערך אחד לכל פרמטר, לפי רכיב:
    { "header": ["דני"], "body": ["בדיקת דם", "09:30"], "header_media_url": null }

מספור פרמטרים הוא פר-רכיב ומתחיל ב-1, בדיוק כמו ב-WhatsApp Cloud API.

רישום ב-main.py:
    from routers.template_manager import router as templates_router
    app.include_router(templates_router, prefix="/api")
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger
import httpx

from routers.phones import _get_host_for_phone, _agent_post


logger = get_logger("templates")

router = APIRouter(prefix="/phones/{phone_id}/templates", tags=["templates"])
templates_router = router  # alias לרישום ב-main.py

BOT_CONFIG_PAGE_KEY = "templates.paging"
DEFAULT_PAGE_SIZE = 10

STATUSES = ("pending", "approved", "rejected", "pause")
CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")
HEADER_FORMATS = ("none", "text", "image", "video", "document")

PARAM_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
NAME_RE = re.compile(r"^[a-z0-9_]{1,120}$")

MAX_BODY_LEN = 1024
MAX_HEADER_LEN = 60
MAX_FOOTER_LEN = 60
MAX_BUTTONS = 3

_SELECT = (
    "id, phone_id, name, category, lang, content, examples, status, "
    "is_published, param_count, provider_template_id, rejected_reason, "
    "created_at, updated_at"
)

# ── 3. עזר — הוסף ליד _default_lang ─────────────────────────────────────────
def _to_jid(raw: str) -> str:
    """
    אותה לוגיקה כמו ב-send.py של ה-Spine.
    LID הוא מזהה ארוך; מספר רגיל קצר יותר. שליחת LID עם הסיומת הלא נכונה
    מתקבלת ע"י WhatsApp אך ההודעה נעלמת בלי message_status.
    """
    jid = (raw or "").strip()
    if "@" in jid:
        return jid
    return f"{jid}@lid" if len(jid) >= 14 else f"{jid}@s.whatsapp.net"



# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_page_size(db: Client) -> int:
    try:
        res = (
            db.table("bot_config")
            .select("value")
            .eq("key", BOT_CONFIG_PAGE_KEY)
            .limit(1)
            .execute()
        )
        if res.data:
            n = int(res.data[0]["value"])
            return max(1, min(100, n))
    except Exception:
        pass
    return DEFAULT_PAGE_SIZE


def _phone_row(db: Client, phone_id: str) -> dict:
    res = (
        db.table("phones")
        .select("id, number, provider, lang, user_id")
        .eq("id", phone_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Phone not found")
    return res.data[0]


def _default_lang(db: Client, phone_id: str, user: Optional[dict]) -> str:
    """שפת ברירת מחדל: קודם מהטלפון, אחרת מהמשתמש, אחרת he."""
    try:
        phone = _phone_row(db, phone_id)
        if phone.get("lang"):
            return str(phone["lang"])
    except HTTPException:
        pass
    if user and user.get("lang"):
        return str(user["lang"])
    return "he"


def _params_of(text: Optional[str]) -> list[int]:
    """מחזיר את מספרי הפרמטרים שמופיעים בטקסט, ממוינים וללא כפילויות."""
    if not text:
        return []
    return sorted({int(m) for m in PARAM_RE.findall(text)})


def _norm_content(raw: Optional[dict]) -> dict:
    src = dict(raw or {})

    header_src = dict(src.get("header") or {})
    header_fmt = str(header_src.get("format") or "none").lower()
    if header_fmt not in HEADER_FORMATS:
        header_fmt = "none"

    footer_src = dict(src.get("footer") or {})
    body_src = dict(src.get("body") or {})

    buttons: list[dict] = []
    for b in (src.get("buttons") or [])[:MAX_BUTTONS]:
        b = dict(b or {})
        buttons.append({
            "type": str(b.get("type") or "quick_reply"),
            "text": str(b.get("text") or "").strip(),
        })

    return {
        "header": {
            "format": header_fmt,
            "text": str(header_src.get("text") or "").strip(),
        },
        "body": {"text": str(body_src.get("text") or "").strip()},
        "footer": {"text": str(footer_src.get("text") or "").strip()},
        "buttons": buttons,
    }


def _norm_examples(raw: Optional[dict]) -> dict:
    src = dict(raw or {})
    return {
        "header": [str(v) for v in (src.get("header") or [])],
        "body": [str(v) for v in (src.get("body") or [])],
        "header_media_url": src.get("header_media_url"),
    }


def _param_map(content: dict) -> dict[str, list[int]]:
    """פרמטרים פר רכיב. footer לא אמור להכיל פרמטרים כלל."""
    return {
        "header": _params_of((content.get("header") or {}).get("text")),
        "body": _params_of((content.get("body") or {}).get("text")),
        "footer": _params_of((content.get("footer") or {}).get("text")),
    }


def _count_params(content: dict) -> int:
    pm = _param_map(content)
    return len(pm["header"]) + len(pm["body"])


def _iss(code: str, **params) -> dict:
    """בעיה אחת בפורמט שהלקוח מתרגם."""
    return {"code": code, "params": params} if params else {"code": code}


def _validate(name: str, lang: str, content: dict, examples: dict) -> list[dict]:
    """ולידציה מלאה. מחזירה codes ולא טקסט — ה-UI תומך ב-he/en/ru/ar."""
    issues: list[dict] = []

    if not NAME_RE.match(name or ""):
        issues.append(_iss("tplErrName"))

    if not (lang or "").strip():
        issues.append(_iss("tplErrLang"))

    header = content.get("header") or {}
    body = content.get("body") or {}
    footer = content.get("footer") or {}

    body_text = (body.get("text") or "").strip()
    if not body_text:
        issues.append(_iss("tplErrBodyRequired"))
    if len(body_text) > MAX_BODY_LEN:
        issues.append(_iss("tplErrBodyLong", max=MAX_BODY_LEN))

    header_fmt = header.get("format") or "none"
    header_text = (header.get("text") or "").strip()
    if header_fmt == "text":
        if not header_text:
            issues.append(_iss("tplErrHeaderRequired"))
        if len(header_text) > MAX_HEADER_LEN:
            issues.append(_iss("tplErrHeaderLong", max=MAX_HEADER_LEN))

    if len((footer.get("text") or "").strip()) > MAX_FOOTER_LEN:
        issues.append(_iss("tplErrFooterLong", max=MAX_FOOTER_LEN))

    pm = _param_map(content)

    if pm["footer"]:
        issues.append(_iss("tplErrFooterParams"))

    if header_fmt == "text" and len(pm["header"]) > 1:
        issues.append(_iss("tplErrHeaderOneParam"))
    if header_fmt != "text" and pm["header"]:
        issues.append(_iss("tplErrHeaderParamsFormat"))

    # מספור רציף שמתחיל ב-1, פר רכיב
    for comp in ("header", "body"):
        nums = pm[comp]
        if nums and nums != list(range(1, len(nums) + 1)):
            issues.append(_iss("tplErrSeq", comp=comp.upper()))

    # כללי WhatsApp: BODY לא מתחיל/מסתיים בפרמטר ולא מורכב מפרמטרים בלבד
    if body_text:
        if PARAM_RE.match(body_text):
            issues.append(_iss("tplErrBodyStartsParam"))
        if re.search(r"\{\{\s*\d+\s*\}\}\s*$", body_text):
            issues.append(_iss("tplErrBodyEndsParam"))
        if not PARAM_RE.sub("", body_text).strip():
            issues.append(_iss("tplErrBodyOnlyParams"))

    # דוגמאות — ערך לכל פרמטר
    for comp in ("header", "body"):
        need = len(pm[comp])
        got = [v for v in (examples.get(comp) or []) if str(v).strip()]
        if need and len(got) < need:
            issues.append(_iss("tplErrExamples", comp=comp.upper(), need=need, got=len(got)))

    if header_fmt in ("image", "video", "document") and not examples.get("header_media_url"):
        issues.append(_iss("tplErrMediaExample"))

    for i, b in enumerate(content.get("buttons") or []):
        if not (b.get("text") or "").strip():
            issues.append(_iss("tplErrButtonText", n=i + 1))

    return issues


def render_preview(content: dict, values: Optional[dict] = None) -> str:
    """מרנדר תצוגה מקדימה. values = { header:[...], body:[...] }."""
    values = values or {}

    def fill(text: str, arr: list) -> str:
        def sub(m):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(arr) and str(arr[idx]).strip():
                return str(arr[idx])
            return m.group(0)
        return PARAM_RE.sub(sub, text or "")

    parts = []
    header = content.get("header") or {}
    if (header.get("format") or "none") == "text" and header.get("text"):
        parts.append(fill(header["text"], values.get("header") or []))
    body = (content.get("body") or {}).get("text") or ""
    if body:
        parts.append(fill(body, values.get("body") or []))
    footer = (content.get("footer") or {}).get("text") or ""
    if footer:
        parts.append(footer)
    return "\n".join(parts)


def _expand(row: dict) -> dict:
    content = row.get("content") or {}
    row["params"] = _param_map(content)
    row["preview"] = render_preview(content, row.get("examples") or {})
    return row


# ══════════════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════════════
class TestSendReq(BaseModel):
    """to = מספר או jid מלא. params ריק → נלקחות הדוגמאות מהתבנית."""
    to:     str
    params: Optional[dict[str, list[str]]] = None

class TemplateCreate(BaseModel):
    name: str
    category: Optional[Literal["UTILITY", "MARKETING", "AUTHENTICATION"]] = "UTILITY"
    lang: Optional[str] = None
    content: Optional[dict[str, Any]] = None
    examples: Optional[dict[str, Any]] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[Literal["UTILITY", "MARKETING", "AUTHENTICATION"]] = None
    lang: Optional[str] = None
    content: Optional[dict[str, Any]] = None
    examples: Optional[dict[str, Any]] = None


class StatusUpdate(BaseModel):
    status: Literal["pending", "approved", "rejected", "pause"]
    rejected_reason: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
# Endpoints — סדר חשוב: נתיבים קבועים לפני /{template_id}
# ══════════════════════════════════════════════════════════════════════════

# ── 4. endpoints — הוסף לפני delete_template ────────────────────────────────

@router.post("/{template_id}/test-send")
async def test_send(
    phone_id: str,
    template_id: str,
    body: TestSendReq,
    db: Client = Depends(get_supabase),
):
    """
    שליחת בדיקה של תבנית מאושרת, גם אם עדיין לא פורסמה.
    הדגל test=true אומר ל-HostAgent לוותר על בדיקת is_published בלבד —
    status חייב להישאר approved.
    """
    if not (body.to or "").strip():
        raise HTTPException(status_code=400, detail="to is required")

    res = (
        db.table("phone_templates")
        .select("id, name, lang, status, is_published, content, examples")
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Template not found")
    row = res.data[0]

    if row.get("status") != "approved":
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "issues": [_iss("tplErrNotApproved", status=row.get("status"))]},
        )

    content = row.get("content") or {}
    issues = _validate(
        row.get("name") or "",
        row.get("lang") or "",
        content,
        row.get("examples") or {},
    )
    if issues:
        raise HTTPException(status_code=422, detail={"ok": False, "issues": issues})

    # ── השלמת פרמטרים חסרים מהדוגמאות ─────────────────────────────────────
    pm = _param_map(content)
    supplied = body.params or {}
    examples = row.get("examples") or {}
    params: dict[str, list[str]] = {}

    for part in ("header", "body"):
        need = len(pm[part])
        given = list(supplied.get(part) or [])
        fallback = list(examples.get(part) or [])
        vals = []
        for i in range(need):
            v = given[i] if i < len(given) and str(given[i]).strip() else None
            if v is None:
                v = fallback[i] if i < len(fallback) else ""
            vals.append(str(v))
        params[part] = vals

    # ── HostAgent ─────────────────────────────────────────────────────────
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=503, detail="No agent available for this phone")

    payload = {
        "jid":        _to_jid(body.to),
        "name":       row["name"],
        "lang":       row["lang"],
        "templateId": row["id"],
        "params":     params,
        "test":       True,
    }

    logger.info(
        f"[TPL] test-send {row['name']}/{row['lang']} → {payload['jid']} "
        f"phone={phone_id} host={host.get('host_name')}"
    )

    try:
        data = await _agent_post(
            host["ip_address"],
            f"/api/phones/{phone_id}/send/template",
            payload,
            timeout=25,
        )
    except httpx.HTTPStatusError as e:
        # ה-HostAgent מחזיר 404/409/400/501 עם detail מפורש
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text[:400])
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Agent unreachable: {e}")

    return {
        "ok":         True,
        "message_id": (data or {}).get("messageId"),
        "jid":        payload["jid"],
        "params":     params,
    }


@router.post("/{template_id}/approve-publish")
async def approve_and_publish(
    phone_id: str,
    template_id: str,
    db: Client = Depends(get_supabase),
):
    """
    baileys: אין גורם חיצוני שמאשר, אז אישור ופרסום הם פעולה אחת.
    whatsapp: האישור מגיע מ-Meta — כאן רק מפרסמים תבנית שכבר approved.
    """
    phone = _phone_row(db, phone_id)
    provider = phone.get("provider") or "baileys"

    existing = (
        db.table("phone_templates")
        .select("id, name, lang, status, content, examples")
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Template not found")
    row = existing.data[0]

    issues = _validate(
        row.get("name") or "",
        row.get("lang") or "",
        row.get("content") or {},
        row.get("examples") or {},
    )

    # רק ב-baileys מותר לקפוץ מעל האישור.
    if provider != "baileys" and row.get("status") != "approved":
        issues.append(_iss("tplErrNotApproved", status=row.get("status")))

    if issues:
        raise HTTPException(status_code=422, detail={"ok": False, "issues": issues})

    result = (
        db.table("phone_templates")
        .update({
            "status":          "approved",
            "is_published":    True,
            "rejected_reason": None,
            "updated_at":      _now(),
        })
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .execute()
    )

    logger.info(f"[TPL] approve+publish {row['name']}/{row['lang']} phone={phone_id}")
    return _expand(result.data[0])
    
@router.get("/")
async def list_templates(
    phone_id: str,
    page: int = 1,
    status: Optional[str] = Query(None),
    lang: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Client = Depends(get_supabase),
):
    page = max(1, page)
    page_size = _get_page_size(db)
    start = (page - 1) * page_size
    end = start + page_size - 1

    query = (
        db.table("phone_templates")
        .select(_SELECT, count="exact")
        .eq("phone_id", phone_id)
    )
    if status and status in STATUSES:
        query = query.eq("status", status)
    if lang:
        query = query.eq("lang", lang)
    if q:
        query = query.ilike("name", f"%{q}%")

    result = query.order("created_at", desc=True).range(start, end).execute()
    total = result.count or 0

    return {
        "items": [_expand(r) for r in (result.data or [])],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/published")
async def list_published(phone_id: str, db: Client = Depends(get_supabase)):
    """לשימוש ה-InputEditor — רק תבניות מאושרות ומפורסמות."""
    result = (
        db.table("phone_templates")
        .select(_SELECT)
        .eq("phone_id", phone_id)
        .eq("is_published", True)
        .eq("status", "approved")
        .order("name")
        .execute()
    )
    return [_expand(r) for r in (result.data or [])]


@router.get("/{template_id}")
async def get_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("phone_templates")
        .select(_SELECT)
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found")
    return _expand(result.data[0])


@router.post("/")
async def create_template(
    phone_id: str,
    body: TemplateCreate,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    phone = _phone_row(db, phone_id)

    content = _norm_content(body.content)
    examples = _norm_examples(body.examples)
    lang = (body.lang or "").strip() or _default_lang(db, phone_id, user)
    name = (body.name or "").strip().lower()

    # baileys אין גורם חיצוני שמאשר — התבנית נכנסת ישר כמאושרת.
    status = "approved" if phone.get("provider") == "baileys" else "pending"

    payload = {
        "id": str(uuid.uuid4()),
        "phone_id": phone_id,
        "name": name,
        "category": body.category or "UTILITY",
        "lang": lang,
        "content": content,
        "examples": examples,
        "status": status,
        "is_published": False,
        "param_count": _count_params(content),
        "created_at": _now(),
        "updated_at": _now(),
    }

    result = db.table("phone_templates").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create template")

    logger.info(f"[TPL] created {name}/{lang} phone={phone_id} status={status}")
    return _expand(result.data[0])


@router.put("/{template_id}")
async def update_template(
    phone_id: str,
    template_id: str,
    body: TemplateUpdate,
    db: Client = Depends(get_supabase),
):
    existing = (
        db.table("phone_templates")
        .select("id, content, examples, is_published, status")
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Template not found")
    row = existing.data[0]

    if row.get("is_published"):
        raise HTTPException(
            status_code=409,
            detail="התבנית מפורסמת — יש לבטל פרסום לפני עריכה",
        )

    payload: dict = {"updated_at": _now()}

    if body.name is not None:
        payload["name"] = body.name.strip().lower()
    if body.category is not None:
        payload["category"] = body.category
    if body.lang is not None:
        payload["lang"] = body.lang.strip()

    if body.content is not None:
        content = _norm_content(body.content)
        payload["content"] = content
        payload["param_count"] = _count_params(content)
        # שינוי תוכן מחזיר את התבנית לבדיקה
        if row.get("status") in ("approved", "rejected"):
            payload["status"] = "pending"
            payload["rejected_reason"] = None
    if body.examples is not None:
        payload["examples"] = _norm_examples(body.examples)

    result = (
        db.table("phone_templates")
        .update(payload)
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found")
    return _expand(result.data[0])


@router.post("/{template_id}/validate")
async def validate_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("phone_templates")
        .select("name, lang, content, examples")
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found")
    row = result.data[0]
    issues = _validate(
        row.get("name") or "",
        row.get("lang") or "",
        row.get("content") or {},
        row.get("examples") or {},
    )
    return {"ok": not issues, "issues": issues}


@router.patch("/{template_id}/status")
async def set_status(
    phone_id: str,
    template_id: str,
    body: StatusUpdate,
    db: Client = Depends(get_supabase),
):
    payload: dict = {"status": body.status, "updated_at": _now()}
    payload["rejected_reason"] = body.rejected_reason if body.status == "rejected" else None

    # סטטוס שאינו approved מבטל פרסום — אין שליחה בתבנית לא מאושרת
    if body.status != "approved":
        payload["is_published"] = False

    result = (
        db.table("phone_templates")
        .update(payload)
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found")
    return _expand(result.data[0])


@router.post("/{template_id}/publish")
async def publish_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    existing = (
        db.table("phone_templates")
        .select("id, name, lang, status, content, examples")
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Template not found")
    row = existing.data[0]

    issues = _validate(
        row.get("name") or "",
        row.get("lang") or "",
        row.get("content") or {},
        row.get("examples") or {},
    )
    if row.get("status") != "approved":
        issues.append(_iss("tplErrNotApproved", status=row.get("status")))

    if issues:
        raise HTTPException(status_code=422, detail={"ok": False, "issues": issues})

    result = (
        db.table("phone_templates")
        .update({"is_published": True, "updated_at": _now()})
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    return _expand(result.data[0])


@router.post("/{template_id}/unpublish")
async def unpublish_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("phone_templates")
        .update({"is_published": False, "updated_at": _now()})
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found")
    return _expand(result.data[0])


@router.delete("/{template_id}")
async def delete_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    existing = (
        db.table("phone_templates")
        .select("is_published")
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Template not found")
    if existing.data[0].get("is_published"):
        raise HTTPException(
            status_code=409, detail="לא ניתן למחוק תבנית מפורסמת — בטל פרסום קודם"
        )

    db.table("phone_templates").delete().eq("id", template_id).eq("phone_id", phone_id).execute()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════
# ולידציית קישור תבנית לתרחיש — נקרא מ-scenarios.py בעת publish (סעיף 11)
# ══════════════════════════════════════════════════════════════════════════

def validate_scenario_templates(
    db: Client,
    phone_id: str,
    canvas: list[dict],
    event_type: Optional[str],
) -> list[dict]:
    """
    תרחיש מסוג scheduler הוא יזום מצד העסק — כל רכיב input חייב לשלוח תבנית
    מאושרת ומפורסמת, עם ערך לכל פרמטר.

    מחזיר רשימת issues בפורמט של _run_publish_checks בתרחישים.
    """
    issues: list[dict] = []

    if (event_type or "scheduler") != "scheduler":
        return issues

    inputs = [c for c in (canvas or []) if c.get("type") == "input"]
    if not inputs:
        return issues

    ids = [c.get("templateId") for c in inputs if c.get("templateId")]
    by_id: dict[str, dict] = {}
    if ids:
        res = (
            db.table("phone_templates")
            .select("id, name, lang, status, is_published, content")
            .eq("phone_id", phone_id)
            .in_("id", list(set(ids)))
            .execute()
        )
        by_id = {r["id"]: r for r in (res.data or [])}

    for comp in inputs:
        cid = comp.get("id")
        tpl_id = comp.get("templateId")
        base = {"source": "template", "compId": cid, "compType": "input"}

        if not tpl_id:
            issues.append({**base, **_iss("tplErrSchedulerNoTemplate")})
            continue

        tpl = by_id.get(tpl_id)
        if not tpl:
            issues.append({**base, **_iss("tplErrTemplateMissing")})
            continue

        if tpl.get("status") != "approved":
            issues.append({**base, **_iss("tplErrTemplateStatus",
                                          name=tpl.get("name"), status=tpl.get("status"))})
        if not tpl.get("is_published"):
            issues.append({**base, **_iss("tplErrTemplateUnpublished", name=tpl.get("name"))})

        pm = _param_map(tpl.get("content") or {})
        supplied = comp.get("templateParams") or {}
        for part in ("header", "body"):
            need = len(pm[part])
            got = [v for v in (supplied.get(part) or []) if str(v).strip()]
            if len(got) < need:
                issues.append({**base, **_iss("tplErrTemplateParams",
                                              comp=part.upper(), name=tpl.get("name"),
                                              need=need, got=len(got))})

    return issues
