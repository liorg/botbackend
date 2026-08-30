# scenarios_router.py
import json
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_supabase
from supabase import Client
from pydantic import BaseModel
from typing import Optional, Any, Literal
import uuid
from routers.template_manager import validate_scenario_templates
from routers.compile_check import _post_worker  # ⬅️ אותו לקוח HTTP + DEV_WORKER_URL שכבר משמש את compile-check

router = APIRouter(prefix="/phones/{phone_id}/scenarios", tags=["scenarios"])

BOT_CONFIG_PAGE_KEY = "scenarios.paging"
DEFAULT_PAGE_SIZE = 10


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

# ── Schemas ────────────────────────────────────────────────────────────────

class ScenarioCreate(BaseModel):
    contact_id: Optional[str] = None
    name: str
    status: Optional[str] = "draft"
    config: Optional[dict] = {}
    estimated_duration_minutes: Optional[str] = None
    inter_leaf_response_time: Optional[str] = None
    canvas: Optional[list[dict[str, Any]]] = None
    arrow_data: Optional[dict[str, Any]] = None
    interval: Optional[dict[str, Any]] = None
    estimated_time: Optional[dict[str, Any]] = None
    use_auto_calc: Optional[bool] = True
    description: Optional[str] = None
    bot_contact: Optional[dict[str, Any]] = None
    event_type: Optional[Literal["trigger", "scheduler"]] = "scheduler"
    priority: Optional[int] = 15


class ScenarioUpdate(BaseModel):
    contact_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    config: Optional[dict] = None
    estimated_duration_minutes: Optional[str] = None
    inter_leaf_response_time: Optional[str] = None
    canvas: Optional[list[dict[str, Any]]] = None
    arrow_data: Optional[dict[str, Any]] = None
    interval: Optional[dict[str, Any]] = None
    estimated_time: Optional[dict[str, Any]] = None
    use_auto_calc: Optional[bool] = None
    description: Optional[str] = None
    bot_contact: Optional[dict[str, Any]] = None
    event_type: Optional[Literal["trigger", "scheduler"]] = None
    priority: Optional[int] = None


def _merge_config(existing_config: dict, body) -> dict:
    cfg = dict(existing_config or {})
    if body.canvas         is not None: cfg["canvas"]         = body.canvas
    if body.arrow_data     is not None: cfg["arrow_data"]     = body.arrow_data
    if body.interval       is not None: cfg["interval"]       = body.interval
    if body.estimated_time is not None: cfg["estimated_time"] = body.estimated_time
    if body.use_auto_calc  is not None: cfg["use_auto_calc"]  = body.use_auto_calc
    if body.description    is not None: cfg["description"]    = body.description
    if body.bot_contact    is not None: cfg["bot_contact"]    = body.bot_contact
    if body.config:
        cfg.update(body.config)
    return cfg


def _expand_config(row: dict) -> dict:
    cfg = row.get("config") or {}
    row["canvas"]         = cfg.get("canvas", [])
    row["arrow_data"]     = cfg.get("arrow_data", {})
    row["interval"]       = cfg.get("interval", {"mins": 0, "secs": 1})
    row["estimated_time"] = cfg.get("estimated_time")
    row["use_auto_calc"]  = cfg.get("use_auto_calc", True)
    row["description"]    = cfg.get("description", "")
    row["bot_contact"]    = cfg.get("bot_contact")
    row["event_type"]     = row.get("event_type") or cfg.get("event_type", "scheduler")
    row["priority"]       = row.get("priority") if row.get("priority") is not None else 15  # ← תוקן: Python, לא JS
    return row


_SELECT = (
    "id, phone_id, contact_id, name, status, config, event_type, priority, "  # ← priority בselect
    "created_at, estimated_duration_minutes, inter_leaf_response_time, "
    "contacts(id, name, number, avatar, is_bot)"
)


# ══════════════════════════════════════════════════════════════════════════
# ── Server-side publish validation ──────────────────────────────────────────
# מראה קוד ל-src/utils/componentTypes.js:isValid — אותה לוגיקה, בפייתון,
# כדי שלקוח שמדלג על הבדיקות (או קורא ל-publish ישירות) לא יוכל לפרסם תרחיש שבור.
# ══════════════════════════════════════════════════════════════════════════

def _comp_is_valid(comp: dict) -> bool:
    t = comp.get("type")
    if t == "input":
        # תבנית מחליפה את הטקסט החופשי
        return bool((comp.get("value") or "").strip()) or bool(comp.get("templateId"))
    if t in ("text", "menu"):
        return bool((comp.get("value") or "").strip())
    if t == "buttons":
        return bool((comp.get("header") or "").strip())
    if t == "button_select":
        return bool((comp.get("buttonId") or "").strip()) and bool((comp.get("buttonText") or "").strip())
    if t == "menu_select":
        return bool((comp.get("menuId") or "").strip()) and bool((comp.get("menuText") or "").strip())
    if t in ("card_sender", "card_expect"):
        return bool((comp.get("code") or "").strip())
    return True
 


async def _check_component_deno(comp: dict) -> Optional[dict]:
    """מריץ /dev/deno/test על עלה בודד. מחזיר issue אם נכשל, אחרת None."""
    card_type = "expect" if comp.get("type") == "card_expect" else "sender"
    try:
        result = await _post_worker("/dev/deno/test", {
            "code":       comp.get("code") or "",
            "card_type":  card_type,
            "timeout_ms": 5000,
        })
    except HTTPException as e:
        return {"source": "deno", "compId": comp.get("id"), "compType": comp.get("type"), "message": str(e.detail)}

    if not result.get("ok"):
        msg = result.get("detail") or result.get("error") or "קוד Deno לא תקין"
        return {"source": "deno", "compId": comp.get("id"), "compType": comp.get("type"), "message": str(msg)}
    return None


async def _run_publish_checks(row: dict, db: Client, phone_id: str) -> list[dict]:
    """ולידציה + Deno per-leaf + Compile מלא — אותו סדר שרץ בצד הלקוח, נאכף עכשיו גם בשרת."""
    issues: list[dict] = []
    cfg    = row.get("config") or {}
    canvas = cfg.get("canvas") or []

    # 1) ולידציית שדות
    for comp in canvas:
        if not _comp_is_valid(comp):
            issues.append({
                "source": "validation", "compId": comp.get("id"), "compType": comp.get("type"),
                "message": "רכיב לא תקין — שדה חובה חסר",
            })

    # 1b) ⬅️ חדש: תרחיש scheduler מחייב תבנית מאושרת+מפורסמת בכל רכיב input
    issues.extend(
        validate_scenario_templates(
            db=db,
            phone_id=phone_id,
            canvas=canvas,
            event_type=row.get("event_type") or cfg.get("event_type", "scheduler"),
        )
    )
 

    # 2) בדיקת קוד Deno לכל card_sender/card_expect עם קוד (מקבילית)
    code_comps = [c for c in canvas if c.get("type") in ("card_sender", "card_expect") and (c.get("code") or "").strip()]
    if code_comps:
        results = await asyncio.gather(*[_check_component_deno(c) for c in code_comps])
        issues.extend([r for r in results if r])

    # 3) קומפילציה מלאה — רק אם 1+2 נקיים
    if not issues:
        scenario_json = json.dumps({
            "fileName":      row.get("name"),
            "description":   cfg.get("description", ""),
            "botContact":    cfg.get("bot_contact"),
            "phone":         (cfg.get("bot_contact") or {}).get("phone", "") if cfg.get("bot_contact") else "",
            "interval":      cfg.get("interval", {"mins": 0, "secs": 1}),
            "estimatedTime": cfg.get("estimated_time"),
            "arrowData":     cfg.get("arrow_data", {}),
            "canvas":        canvas,
            "eventType":     row.get("event_type") or cfg.get("event_type", "scheduler"),
        }, ensure_ascii=False)

        try:
            compile_res = await _post_worker("/dev/scenario/compile", {"scenario_json": scenario_json})
        except HTTPException as e:
            issues.append({"source": "compile", "compId": None, "compType": None, "message": str(e.detail)})
        else:
            if not compile_res.get("ok"):
                msgs = compile_res.get("error") or "קומפילציה נכשלה"
                issues.append({"source": "compile", "compId": None, "compType": None, "message": str(msgs)})
            for w in (compile_res.get("warnings") or []):
                issues.append({"source": "compile", "compId": None, "compType": None, "message": f"⚠ {w}"})

    return issues


# ── List scenarios ─────────────────────────────────────────────────────────
# ── List scenarios (paginated) ─────────────────────────────────────────────
@router.get("/")
async def list_scenarios(
    phone_id: str,
    page: int = 1,
    db: Client = Depends(get_supabase),
):
    page = max(1, page)
    page_size = _get_page_size(db)
    start = (page - 1) * page_size
    end = start + page_size - 1

    result = (
        db.table("scenarios")
        .select(_SELECT, count="exact")
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .range(start, end)
        .execute()
    )

    total = result.count or 0
    return {
        "items": [_expand_config(r) for r in (result.data or [])],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }

# ── List by event_type ─────────────────────────────────────────────────────
@router.get("/by-type/{event_type}")
async def list_scenarios_by_type(
    phone_id: str,
    event_type: Literal["trigger", "scheduler"],
    db: Client = Depends(get_supabase)
):
    result = (
        db.table("scenarios")
        .select(_SELECT)
        .eq("phone_id", phone_id)
        .eq("event_type", event_type)
        .eq("status", "active")
        .order("created_at", desc=True)
        .execute()
    )
    return [_expand_config(r) for r in (result.data or [])]


# ── Get one ────────────────────────────────────────────────────────────────
@router.get("/{scenario_id}")
async def get_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    result = (
        db.table("scenarios")
        .select(_SELECT)
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _expand_config(result.data)


# ── Create ─────────────────────────────────────────────────────────────────
@router.post("/")
async def create_scenario(
    phone_id: str, body: ScenarioCreate, db: Client = Depends(get_supabase)
):
    config = _merge_config({}, body)

    payload = {
        "id":         str(uuid.uuid4()),
        "phone_id":   phone_id,
        "name":       body.name,
        "status":     body.status or "draft",
        "config":     config,
        "event_type": body.event_type or "scheduler",
        "priority":   body.priority if body.priority is not None else 15,
    }
    if body.contact_id:                 payload["contact_id"]                 = body.contact_id
    if body.estimated_duration_minutes: payload["estimated_duration_minutes"] = body.estimated_duration_minutes
    if body.inter_leaf_response_time:   payload["inter_leaf_response_time"]   = body.inter_leaf_response_time

    result = db.table("scenarios").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create scenario")
    return _expand_config(result.data[0])


# ── Update ─────────────────────────────────────────────────────────────────
@router.put("/{scenario_id}")
async def update_scenario(
    phone_id: str, scenario_id: str, body: ScenarioUpdate,
    db: Client = Depends(get_supabase)
):
    existing = (
        db.table("scenarios")
        .select("config")
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .single()
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Scenario not found")

    config = _merge_config(existing.data.get("config") or {}, body)

    payload: dict = {"config": config}
    if body.name       is not None: payload["name"]       = body.name
    if body.status     is not None: payload["status"]     = body.status
    if body.contact_id is not None: payload["contact_id"] = body.contact_id
    if body.event_type is not None: payload["event_type"] = body.event_type
    if body.priority   is not None: payload["priority"]   = body.priority
    if body.estimated_duration_minutes is not None:
        payload["estimated_duration_minutes"] = body.estimated_duration_minutes
    if body.inter_leaf_response_time is not None:
        payload["inter_leaf_response_time"] = body.inter_leaf_response_time

    result = (
        db.table("scenarios")
        .update(payload)
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _expand_config(result.data[0])


# ── Publish ────────────────────────────────────────────────────────────────
@router.post("/{scenario_id}/publish")
async def publish_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    # ⬅️ שולפים את השורה המלאה (כולל config) — לא רק לבדוק שהיא קיימת
    existing = (
        db.table("scenarios")
        .select("id, name, event_type, config")
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .single()
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Scenario not found")

    # ⬅️ ולידציה + Deno + Compile — נאכף בשרת, לא רק ב-UI
    issues = await _run_publish_checks(existing.data)
    if issues:
        raise HTTPException(status_code=422, detail={"ok": False, "issues": issues})

    result = (
        db.table("scenarios")
        .update({"status": "active"})
        .eq("id", scenario_id)
        .eq("phone_id", phone_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _expand_config(result.data[0])


# ── Delete ─────────────────────────────────────────────────────────────────
@router.delete("/{scenario_id}")
async def delete_scenario(
    phone_id: str, scenario_id: str, db: Client = Depends(get_supabase)
):
    db.table("scenarios").delete().eq("id", scenario_id).eq("phone_id", phone_id).execute()
    return {"ok": True}
