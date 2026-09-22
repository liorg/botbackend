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
from pydantic import BaseModel, Field

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger
import httpx

#from routers.phones import _get_host_for_phone, _agent_post

from urllib.parse import quote

from routers.phones import _get_host_for_phone, _agent_post, _agent_delete
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

def _to_components(content: dict, examples: Optional[dict] = None) -> list[dict]:
    """content{header,body,footer,buttons} → components[] בפורמט WhatsApp.
    ההפך המדויק של ToTemplateContent ב-TemplatesController."""
    examples = examples or {}
    comps: list[dict] = []

    h   = content.get("header") or {}
    fmt = (h.get("format") or "none").lower()
    if fmt != "none":
        comp = {"type": "HEADER", "format": fmt.upper()}
        if fmt == "text":
            comp["text"] = h.get("text") or ""
            if examples.get("header"):
                comp["example"] = {"header_text": list(examples["header"])}
        comps.append(comp)

    b = content.get("body") or {}
    if b.get("text"):
        comp = {"type": "BODY", "text": b["text"]}
        if examples.get("body"):
            comp["example"] = {"body_text": [list(examples["body"])]}
        comps.append(comp)

    f = content.get("footer") or {}
    if f.get("text"):
        comps.append({"type": "FOOTER", "text": f["text"]})

    buttons = content.get("buttons") or []
    if buttons:
        comps.append({"type": "BUTTONS", "buttons": [
            {"type": (btn.get("type") or "quick_reply").upper(), "text": btn.get("text") or ""}
            for btn in buttons
        ]})

    return comps


async def _register_with_manager(db: Client, phone_id: str, name: str, lang: str,
                                 category: str, content: dict, examples: dict) -> dict:
    """POST /api/phones/{id}/templates ב-Manager — הוא קורא ל-whatsapp-single
    ומעדכן את הטיוטה (provider_template_id + status)."""
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=503, detail="No agent available for this phone")

    payload = {
        "name":       name,
        "language":   lang,
        "category":   category,
        "components": _to_components(content, examples),
    }
    try:
        return await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/templates", payload, timeout=25)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text[:400])
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Manager unreachable: {e}")

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
    to: str = Field(..., description="Recipient number or full JID.")
    params: Optional[dict[str, list[str]]] = Field(None, description="Values per part: { header: [...], body: [...] }. Empty values fall back to the examples.")

class TemplateCreate(BaseModel):
    name: str = Field(..., description="Template name: lowercase letters, digits and underscores.")
    category: Optional[Literal["UTILITY", "MARKETING", "AUTHENTICATION"]] = Field("UTILITY", description="UTILITY, MARKETING or AUTHENTICATION.")
    lang: Optional[str] = Field(None, description="Language code, for example en_US. Defaults to the phone's language, then the user's.")
    content: Optional[dict[str, Any]] = Field(None, description="Template parts: header, body, footer, buttons. Parameters use {{1}}, {{2}} per part.")
    examples: Optional[dict[str, Any]] = Field(None, description="One example per parameter, per part: { header, body, header_media_url }.")


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Template name: lowercase letters, digits and underscores.")
    category: Optional[Literal["UTILITY", "MARKETING", "AUTHENTICATION"]] = Field(None, description="UTILITY, MARKETING or AUTHENTICATION.")
    lang: Optional[str] = Field(None, description="Language code, for example en_US.")
    content: Optional[dict[str, Any]] = Field(None, description="Template parts. Changing them sends an approved or rejected template back to pending.")
    examples: Optional[dict[str, Any]] = Field(None, description="One example per parameter, per part: { header, body, header_media_url }.")


class StatusUpdate(BaseModel):
    status: Literal["pending", "approved", "rejected", "pause"] = Field(..., description="pending, approved, rejected or pause.")
    rejected_reason: Optional[str] = Field(None, description="Reason stored when status is rejected; cleared otherwise.")


# ══════════════════════════════════════════════════════════════════════════
# Endpoints — סדר חשוב: נתיבים קבועים לפני /{template_id}
# ══════════════════════════════════════════════════════════════════════════

# ── 4. endpoints — הוסף לפני delete_template ────────────────────────────────

@router.post("/{template_id}/test-send", summary="Test-send template", description="Sends an approved template to a number, even before it is published. Missing parameters are filled from the template examples.")
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

    # ── השלמת פרמטרים חסרים מהדוגמאות ─────────────────────────────────────
    # הסדר כאן חשוב: קודם מרכיבים את הפרמטרים, ורק אז מוולידים. בשליחת
    # בדיקה הערכים מגיעים מהבקשה, ולכן דוגמאות שמורות אינן תנאי.
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

    issues = _validate(
        row.get("name") or "",
        row.get("lang") or "",
        content,
        row.get("examples") or {},
    )

    # כל פרמטר קיבל ערך? אז tplErrExamples לא רלוונטי כאן. הוא כן נשאר
    # חוסם ב-publish, שם מטא היא זו שדורשת דוגמאות.
    all_filled = all(
        len([v for v in params.get(part, []) if str(v).strip()]) >= len(pm[part])
        for part in ("header", "body")
    )
    if all_filled:
        issues = [i for i in issues if i.get("code") != "tplErrExamples"]

    if issues:
        raise HTTPException(status_code=422, detail={"ok": False, "issues": issues})

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


@router.post("/{template_id}/approve-publish", summary="Approve and publish template", description="Publishes a template that the provider has already approved.")
async def approve_and_publish(
    phone_id: str,
    template_id: str,
    db: Client = Depends(get_supabase),
):
    """האישור מגיע מהספק בלבד — כאן רק מפרסמים תבנית שכבר approved."""
    _phone_row(db, phone_id)   # 404 לטלפון לא קיים

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
    
@router.get("/", summary="List templates", description="Returns the phone's templates, newest first, with optional status, lang and name filters. Page size comes from bot_config 'templates.paging'.")
async def list_templates(
    phone_id: str,
    page: int = Query(1, description="Page number, starting at 1."),
    status: Optional[str] = Query(None, description="Filter by status."),
    lang: Optional[str] = Query(None, description="Filter by language code."),
    q: Optional[str] = Query(None, description="Search in the template name."),
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


@router.get("/published", summary="List published templates", description="Returns approved and published templates ordered by name, for the scenario InputEditor.")
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


@router.get("/{template_id}", summary="Get template", description="Returns one template with its parameter map and preview.")
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


@router.post("/", summary="Create template", description="Creates a template as a local draft only. Nothing is sent to the Manager until the template is published.")
async def create_template(
    phone_id: str,
    body: TemplateCreate,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    _phone_row(db, phone_id)   # 404 לטלפון לא קיים

    content = _norm_content(body.content)
    examples = _norm_examples(body.examples)
    lang = (body.lang or "").strip() or _default_lang(db, phone_id, user)
    name = (body.name or "").strip().lower()

    # נכנס תמיד כטיוטה — ה-Manager הוא שקובע status ו-provider_template_id
    payload = {
        "id":           str(uuid.uuid4()),
        "phone_id":     phone_id,
        "name":         name,
        "category":     body.category or "UTILITY",
        "lang":         lang,
        "content":      content,
        "examples":     examples,
        "status":       "pending",
        "is_published": False,
        "param_count":  _count_params(content),
        "created_at":   _now(),
        "updated_at":   _now(),
    }

    result = db.table("phone_templates").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create template")
    row = result.data[0]

    # שמירה בלבד — אין שליחה ל-Manager. הרישום מתבצע ב-/publish.
    logger.info(f"[TPL] created draft {name}/{lang} phone={phone_id}")
    return _expand(row)


@router.put("/{template_id}", summary="Update template", description="Updates an unpublished template. Changing content moves an approved or rejected template back to pending. Returns 409 when the template is published.")
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


@router.post("/{template_id}/validate", summary="Validate template", description="Checks the template and returns { ok, issues } without changing it.")
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


@router.patch("/{template_id}/status", summary="Set template status", description="Sets pending, approved, rejected or pause. Any status other than approved also unpublishes the template.")
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


@router.post("/{template_id}/publish", summary="Publish template", description="Validates the template, registers it through the Manager, and stores the status the provider returns. Published only when the provider approves.")
async def publish_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    existing = (
        db.table("phone_templates")
        .select("id, name, lang, category, status, content, examples, provider_template_id")
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
    if issues:
        raise HTTPException(status_code=422, detail={"ok": False, "issues": issues})

    # השליחה ל-Manager מתבצעת כאן בלבד — לא בשמירה
    reg = await _register_with_manager(
        db,
        phone_id,
        row.get("name") or "",
        row.get("lang") or "",
        row.get("category") or "UTILITY",
        row.get("content") or {},
        row.get("examples") or {},
    ) or {}

    # מה שחוזר מה-Manager קובע status / provider id
    status = str(reg.get("status") or "pending").lower()
    if status not in STATUSES:
        status = "pending"

    payload: dict = {
        "status":          status,
        "is_published":    status == "approved",
        "rejected_reason": (reg.get("rejected_reason") or reg.get("reason")) if status == "rejected" else None,
        "updated_at":      _now(),
    }
    provider_id = reg.get("id") or reg.get("provider_template_id")
    if provider_id:
        payload["provider_template_id"] = str(provider_id)

    result = (
        db.table("phone_templates")
        .update(payload)
        .eq("id", template_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found")

    logger.info(f"[TPL] publish {row.get('name')}/{row.get('lang')} phone={phone_id} "
                f"provider_id={provider_id} status={status}")
    return _expand(result.data[0])


@router.post("/{template_id}/unpublish", summary="Unpublish template", description="Marks the template as not published.")
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


@router.delete("/{template_id}", summary="Delete template", description="Deletes an unpublished template. Registered templates are deleted through the Manager first. Returns 409 when the template is published.")
async def delete_template(
    phone_id: str, template_id: str, db: Client = Depends(get_supabase)
):
    existing = (
        db.table("phone_templates")
        .select("is_published, provider_template_id, name, lang")
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
            status_code=409, detail="לא ניתן למחוק תבנית מפורסמת — בטל פרסום קודם"
        )

    provider_id = row.get("provider_template_id")

    # טיוטה שלא נרשמה מעולם — מחיקה מקומית בלבד
    if not provider_id:
        db.table("phone_templates").delete().eq("id", template_id).eq("phone_id", phone_id).execute()
        logger.info(f"[TPL] deleted draft {row.get('name')}/{row.get('lang')} phone={phone_id}")
        return {"ok": True, "provider_deleted": False}

    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=503, detail="No agent available for this phone")

    path = f"/api/phones/{phone_id}/templates/{quote(str(provider_id), safe='')}"
    try:
        await _agent_delete(host["ip_address"], path, timeout=25)
        logger.info(f"[TPL] deleted {row.get('name')}/{row.get('lang')} "
                    f"phone={phone_id} provider_id={provider_id}")
        return {"ok": True, "provider_deleted": True}   # ה-Manager כבר מחק את הרשומה
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text[:400])
        logger.warning(f"[TPL] Manager 404 for provider_id={provider_id} — מנקה מקומית")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Manager unreachable: {e}")

    db.table("phone_templates").delete().eq("id", template_id).eq("phone_id", phone_id).execute()
    return {"ok": True, "provider_deleted": False}
# ══════════════════════════════════════════════════════════════════════════
# ולידציית קישור תבנית לתרחיש — נקרא מ-scenarios.py בעת publish (סעיף 11)
# ══════════════════════════════════════════════════════════════════════════
def validate_scenario_templates(
    db: Client,
    phone_id: str,
    canvas: list[dict],
    event_type: str,
) -> list[dict]:

    issues = []

    # רק scheduler דורש Template בהודעה הראשונה
    if event_type != "scheduler":
        return issues

    first_send = next(
        (
            comp for comp in canvas
            if comp.get("type") == "input"
            and comp.get("side") == "send"
        ),
        None,
    )

    if not first_send:
        return issues

    template_id = first_send.get("templateId")

    if not template_id:
        issues.append({
            "source": "template",
            "compId": first_send.get("id"),
            "compType": first_send.get("type"),
            "code": "tplErrSchedulerNoTemplate",
        })
        return issues

    # מכאן ממשיכות הבדיקות הקיימות שלך:
    # template קיים
    # שייך ל-phone
    # published
    # approved
    # וכו'

    return issues
    
def validate_scenario_templates2(
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


# ══════════════════════════════════════════════════════════════════════════
# Seed — check_contact בלבד. שאר התבניות מגיעות מהספק דרך ImportOnceAsync.
# ══════════════════════════════════════════════════════════════════════════

# NAME_RE allows [a-z0-9_] only, so "CheckContact" is not a legal name.
CHECK_CONTACT_NAME = "check_contact"
CHECK_CONTACT_LANG = "en_US"

CHECK_CONTACT_CONTENT: dict = {
    "header": {"format": "none", "text": ""},
    "body": {"text": "Are you a bot?"},
    "footer": {"text": ""},
    "buttons": [],
}

CHECK_CONTACT_EXAMPLES: dict = {"header": [], "body": [], "header_media_url": None}

# Templates seeded on provision. Creation goes through the agent proxy;
# only the existence check runs against the DB.
SEED_TEMPLATES: list[dict] = [
    {
        "name":     CHECK_CONTACT_NAME,
        "lang":     CHECK_CONTACT_LANG,
        "content":  CHECK_CONTACT_CONTENT,
        "examples": CHECK_CONTACT_EXAMPLES,
        "category": "UTILITY",
    },
]


def find_template(db: Client, phone_id: str, name: str, lang: str) -> Optional[dict]:
    """DB lookup only. Returns the row if it exists, else None."""
    result = (
        db.table("phone_templates")
        .select(_SELECT)
        .eq("phone_id", phone_id)
        .eq("name", name)
        .eq("lang", lang)
        .limit(1)
        .execute()
    )
    return _expand(result.data[0]) if result.data else None


def list_templates(db: Client, phone_id: str) -> list[dict]:
    result = (
        db.table("phone_templates")
        .select(_SELECT)
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [_expand(r) for r in (result.data or [])]


def supports_templates(db: Client, phone_id: str) -> bool:
    phone = _phone_row(db, phone_id)
    return (phone.get("provider") or "baileys") == "baileys"


# PING message template — לפי סדר העדפה, ואז כל תבנית מאושרת.
# check_contact הוא ה-seed; hello_world מגיע מקטלוג הספק (ImportOnceAsync).
HELLO_WORLD_NAME = "hello_world"
PING_TEMPLATE_PREFERENCE = (CHECK_CONTACT_NAME, HELLO_WORLD_NAME)


def pick_ping_template(db: Client, phone_id: str) -> Optional[dict]:
    """
    check_contact עדיף, אחריו hello_world. אם אף אחד מהם אינו קיים —
    כל תבנית מאושרת ומפורסמת אחרת, בעדיפות לתבנית ללא פרמטרים:
    ל-PING אין ערכים למלא בהם placeholders.
    """
    result = (
        db.table("phone_templates")
        .select(_SELECT)
        .eq("phone_id", phone_id)
        .eq("status", "approved")
        .eq("is_published", True)
        .order("name")
        .execute()
    )

    rows = result.data or []
    if not rows:
        return None

    by_name = {r.get("name"): r for r in rows}
    for name in PING_TEMPLATE_PREFERENCE:
        if name in by_name:
            return _expand(by_name[name])

    param_free = [r for r in rows if not (r.get("param_count") or 0)]
    chosen = (param_free or rows)[0]

    logger.info(f"[TPL] ping fallback → {chosen.get('name')}/{chosen.get('lang')} phone={phone_id}")
    return _expand(chosen)
