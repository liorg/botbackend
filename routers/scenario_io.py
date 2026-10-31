# routers/scenario_io.py
"""
יבוא/יצוא תרחישים של SplitDesigner.

metadata.templates קיים רק בקובץ — הוא לא נשמר ב-DB ולא מגיע ל-Worker.
commit מתרגם אותו להחלטות על תבניות, מעדכן את templateId/Name/Lang ב-canvas, וזורק אותו.
שמות השדות ב-canvas לא משתנים, ולכן IcrConverter / ScenarioEngine לא מושפעים.

רישום ב-main.py — כמו scenarios:
    from routers.scenario_io import router as scenario_io_router
    app.include_router(scenario_io_router, prefix="/api")

נתיבים:
    POST /phones/{phone_id}/scenarios/io/export-metadata   canvas → metadata לקובץ
    POST /phones/{phone_id}/scenarios/io/preview           קובץ → התנגשות שם + החלטות תבניות (בלי כתיבה)
    POST /phones/{phone_id}/scenarios/io/commit            override | new
"""

import hashlib
import json
import re
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_supabase
from logging_config import get_logger
from routers.scenarios import _SELECT, _expand_config, ScenarioResponse, DEFAULT_INTERVAL
from routers.template_manager import (
    CATEGORIES,
    _count_params,
    _norm_content,
    _norm_examples,
    _now,
    _param_map,
    _phone_row,
    _validate,
    publish_template,
)

logger = get_logger("scenario_io")

router = APIRouter(prefix="/phones/{phone_id}/scenarios/io", tags=["scenarios"])

META_VERSION = 1
_TPL_SELECT = "id, name, lang, category, content, examples, status, is_published"
_EVENT_TYPES = ("trigger", "scheduler")


# ══════════════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════════════

class ExportMetaReq(BaseModel):
    canvas: list[dict[str, Any]] = Field(default_factory=list, description="Designer canvas; template ids are taken from its components.")


class ExportMetaResponse(BaseModel):
    metadata: dict[str, Any] = Field(..., description="{ version, exportedAt, templates: { <templateId>: {...} } }.")
    missing: list[str] = Field([], description="Template ids used in the canvas that do not exist on this phone.")


class ImportPreviewReq(BaseModel):
    scenario: dict[str, Any] = Field(..., description="The exported file as is.")


class ImportCommitReq(BaseModel):
    scenario: dict[str, Any] = Field(..., description="The exported file as is.")
    mode: Literal["override", "new"] = Field(..., description="override replaces target_scenario_id and unpublishes it; new creates a draft.")
    target_scenario_id: Optional[str] = Field(None, description="Required for override.")
    name: Optional[str] = Field(None, description="Name for mode=new. Made unique on the phone.")


class ImportPreviewResponse(BaseModel):
    name: str = Field(..., description="Scenario name from the file.")
    steps: int = Field(..., description="Components in the canvas.")
    event_type: str = Field(..., description="trigger or scheduler.")
    has_metadata: bool = Field(..., description="False for files exported before metadata existed.")
    conflict: Optional[dict[str, Any]] = Field(None, description="{ id, name, status } of the scenario with the same name on this phone.")
    suggested_name: str = Field(..., description="Free name for mode=new.")
    templates: list[dict[str, Any]] = Field([], description="Decision per template: matched_id | matched_signature | create | invalid | missing.")


class ImportCommitResponse(BaseModel):
    mode: str = Field(..., description="override or new.")
    scenario: ScenarioResponse = Field(..., description="The written scenario, always draft.")
    templates: list[dict[str, Any]] = Field([], description="Decisions, with targetId and publish result for created templates.")


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

def _signature(content: Optional[dict]) -> str:
    """חתימת תוכן אחרי נרמול — header/body/footer/buttons. ה-id והשם לא משתתפים."""
    raw = json.dumps(_norm_content(content), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _template_ids(canvas: list) -> list[str]:
    out: list[str] = []
    for c in canvas or []:
        tid = (c or {}).get("templateId")
        if tid and tid not in out:
            out.append(str(tid))
    return out


def _phone_templates(db: Client, phone_id: str) -> list[dict]:
    return (
        db.table("phone_templates").select(_TPL_SELECT).eq("phone_id", phone_id).execute().data
        or []
    )


def _meta_entry(row: dict) -> dict:
    content = _norm_content(row.get("content"))
    pm = _param_map(content)
    return {
        "id":         row["id"],
        "name":       row.get("name") or "",
        "lang":       row.get("lang") or "",
        "category":   row.get("category") or "UTILITY",
        "content":    content,
        "examples":   _norm_examples(row.get("examples")),
        "paramCount": {"header": len(pm["header"]), "body": len(pm["body"])},
        "signature":  _signature(content),
    }


def _check_file(scenario: Any) -> str:
    """מבנה בסיסי בלבד. הוולידציה המלאה נשארת ב-publish."""
    if not isinstance(scenario, dict):
        raise HTTPException(status_code=422, detail="File is not a scenario object")
    canvas = scenario.get("canvas")
    if not isinstance(canvas, list):
        raise HTTPException(status_code=422, detail="canvas is missing")
    for i, c in enumerate(canvas):
        if not isinstance(c, dict) or not c.get("id") or not c.get("type"):
            raise HTTPException(status_code=422, detail=f"canvas[{i}] has no id/type")
    return str(scenario.get("fileName") or "").strip() or "scenario"


def _event_type(scenario: dict) -> str:
    et = scenario.get("eventType")
    return et if et in _EVENT_TYPES else "scheduler"


def _scenario_names(db: Client, phone_id: str) -> list[dict]:
    return (
        db.table("scenarios")
        .select("id, name, status, created_at")
        .eq("phone_id", phone_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )


def _free_scenario_name(rows: list[dict], name: str) -> str:
    taken = {r.get("name") for r in rows}
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def _free_template_name(name: str, lang: str, taken: set) -> str:
    """WhatsApp: שם+שפה ייחודיים ב-WABA. NAME_RE = [a-z0-9_]{1,120}."""
    base = re.sub(r"[^a-z0-9_]", "_", (name or "template").lower())[:110] or "template"
    if (base, lang) not in taken:
        return base
    n = 2
    while (f"{base}_{n}", lang) in taken:
        n += 1
    return f"{base}_{n}"


def _resolve(db: Client, phone_id: str, scenario: dict) -> list[dict]:
    """
    החלטה לכל templateId ב-canvas, בלי כתיבה:
      matched_id         — אותו id קיים בטלפון הזה
      matched_signature  — אותו תוכן + אותה שפה (עדיפות לאותו שם, מפורסמת, מאושרת)
      create             — תיווצר עם id חדש ותפורסם
      invalid            — ב-metadata, אבל לא עוברת _validate → לא תיווצר
      missing            — לא בטלפון ולא ב-metadata (קובץ ישן)
    """
    canvas = scenario.get("canvas") or []
    ids = _template_ids(canvas)
    if not ids:
        return []

    meta = ((scenario.get("metadata") or {}).get("templates")) or {}
    own = _phone_templates(db, phone_id)
    by_id = {r["id"]: r for r in own}
    taken = {(r.get("name"), r.get("lang")) for r in own}
    sig_cache = {r["id"]: _signature(r.get("content")) for r in own}

    out: list[dict] = []
    for tid in ids:
        src = meta.get(tid) if isinstance(meta.get(tid), dict) else None
        comp = next(c for c in canvas if c.get("templateId") == tid)
        name = (src or {}).get("name") or comp.get("templateName") or ""
        lang = (src or {}).get("lang") or comp.get("templateLang") or ""
        d: dict = {"sourceId": tid, "name": name, "lang": lang}

        row = by_id.get(tid)
        if row:
            out.append({**d, "action": "matched_id", "targetId": row["id"],
                        "targetName": row.get("name"), "targetLang": row.get("lang"),
                        "status": row.get("status"), "isPublished": bool(row.get("is_published"))})
            continue

        if not src:
            out.append({**d, "action": "missing"})
            continue

        sig = _signature(src.get("content"))
        cands = [r for r in own if r.get("lang") == lang and sig_cache[r["id"]] == sig]
        cands.sort(key=lambda r: (r.get("name") != name,
                                  not r.get("is_published"),
                                  r.get("status") != "approved"))
        if cands:
            r = cands[0]
            out.append({**d, "action": "matched_signature", "targetId": r["id"],
                        "targetName": r.get("name"), "targetLang": r.get("lang"),
                        "status": r.get("status"), "isPublished": bool(r.get("is_published"))})
            continue

        new_name = _free_template_name(name, lang, taken)
        taken.add((new_name, lang))
        issues = _validate(new_name, lang,
                           _norm_content(src.get("content")),
                           _norm_examples(src.get("examples")))
        out.append({**d, "action": "invalid" if issues else "create",
                    "targetName": new_name, "targetLang": lang, "issues": issues})
    return out


def _create_template(db: Client, phone_id: str, name: str, lang: str, src: dict) -> dict:
    """אותה צורה כמו create_template — pending, לא מפורסמת."""
    content = _norm_content(src.get("content"))
    category = src.get("category") if src.get("category") in CATEGORIES else "UTILITY"
    payload = {
        "id":           str(uuid.uuid4()),
        "phone_id":     phone_id,
        "name":         name,
        "category":     category,
        "lang":         lang,
        "content":      content,
        "examples":     _norm_examples(src.get("examples")),
        "status":       "pending",
        "is_published": False,
        "param_count":  _count_params(content),
        "created_at":   _now(),
        "updated_at":   _now(),
    }
    res = db.table("phone_templates").insert(payload).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail=f"Failed to create template {name}")
    logger.info(f"[IO] created template {name}/{lang} phone={phone_id}")
    return res.data[0]


def _detail_text(detail: Any) -> str:
    if isinstance(detail, dict) and isinstance(detail.get("issues"), list):
        return ", ".join(str(i.get("code") or i) for i in detail["issues"])
    return str(detail)[:300]


async def _try_publish(db: Client, phone_id: str, template_id: str) -> dict:
    """אותו מסלול כמו כפתור הפרסום. כשל לא מפיל את היבוא — התבנית נשארת טיוטה."""
    try:
        row = await publish_template(phone_id, template_id, db)
        return {"status": row.get("status"), "isPublished": bool(row.get("is_published")),
                "publishError": None}
    except HTTPException as e:
        logger.warning(f"[IO] publish failed template={template_id}: {e.detail}")
        return {"status": "pending", "isPublished": False, "publishError": _detail_text(e.detail)}


def _rewrite_canvas(canvas: list, id_map: dict) -> list:
    """מחליף רק ערכים — templateId/Name/Lang. templateParams לא נוגעים."""
    out = json.loads(json.dumps(canvas))
    for c in out:
        hit = id_map.get(c.get("templateId"))
        if hit:
            c["templateId"], c["templateName"], c["templateLang"] = hit
    return out


def _valid_contact(db: Client, phone_id: str, bot_contact: Any) -> Optional[str]:
    """contact_id מטלפון אחר הוא FK שבור — נשמר רק אם קיים בטלפון הזה."""
    cid = bot_contact.get("id") if isinstance(bot_contact, dict) else None
    if not cid:
        return None
    try:
        res = (
            db.table("contacts").select("id")
            .eq("id", cid).eq("phone_id", phone_id).limit(1).execute()
        )
    except Exception:
        return None
    return cid if res.data else None


def _build_config(existing: dict, sc: dict, canvas: list, bot_contact: Optional[dict]) -> dict:
    cfg = dict(existing or {})
    cfg.pop("metadata", None)   # לעולם לא ב-DB
    cfg.update({
        "canvas":         canvas,
        "arrow_data":     sc.get("arrowData") or {},
        "interval":       sc.get("interval") or DEFAULT_INTERVAL,
        "estimated_time": sc.get("estimatedTime"),
        "use_auto_calc":  cfg.get("use_auto_calc", True),
        "description":    sc.get("description") or "",
        "bot_contact":    bot_contact,
    })
    return cfg


# ══════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════

@router.post("/export-metadata", response_model=ExportMetaResponse, summary="Template metadata for export", description="Returns the definitions of the phone templates used in the canvas, to embed as metadata in the exported file.")
def export_metadata(phone_id: str, body: ExportMetaReq, db: Client = Depends(get_supabase)):
    _phone_row(db, phone_id)
    ids = _template_ids(body.canvas)
    templates: dict[str, dict] = {}
    if ids:
        rows = (
            db.table("phone_templates").select(_TPL_SELECT)
            .eq("phone_id", phone_id).in_("id", ids).execute().data
            or []
        )
        templates = {r["id"]: _meta_entry(r) for r in rows}
    return {
        "metadata": {"version": META_VERSION, "exportedAt": _now(), "templates": templates},
        "missing":  [i for i in ids if i not in templates],
    }


@router.post("/preview", response_model=ImportPreviewResponse, summary="Preview scenario import", description="Checks the file against the phone without writing: same-name scenario and a decision per template.")
def import_preview(phone_id: str, body: ImportPreviewReq, db: Client = Depends(get_supabase)):
    _phone_row(db, phone_id)
    name = _check_file(body.scenario)
    rows = _scenario_names(db, phone_id)
    hit = next((r for r in rows if r.get("name") == name), None)
    return {
        "name":           name,
        "steps":          len(body.scenario.get("canvas") or []),
        "event_type":     _event_type(body.scenario),
        "has_metadata":   isinstance((body.scenario.get("metadata") or {}).get("templates"), dict),
        "conflict":       {"id": hit["id"], "name": hit["name"], "status": hit.get("status")} if hit else None,
        "suggested_name": _free_scenario_name(rows, name),
        "templates":      _resolve(db, phone_id, body.scenario),
    }


@router.post("/commit", response_model=ImportCommitResponse, summary="Import scenario", description="Resolves templates (creates and publishes missing ones), rewrites template references in the canvas, drops metadata and writes the scenario as draft. override replaces the target and unpublishes it.")
async def import_commit(phone_id: str, body: ImportCommitReq, db: Client = Depends(get_supabase)):
    _phone_row(db, phone_id)
    file_name = _check_file(body.scenario)
    sc = body.scenario

    # ── יעד ────────────────────────────────────────────────────────────────
    target = None
    if body.mode == "override":
        if not body.target_scenario_id:
            raise HTTPException(status_code=422, detail="target_scenario_id is required for override")
        res = (
            db.table("scenarios").select("id, name, config")
            .eq("id", body.target_scenario_id).eq("phone_id", phone_id).limit(1).execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Scenario not found")
        target = res.data[0]
        name = target.get("name") or file_name
    else:
        name = _free_scenario_name(_scenario_names(db, phone_id),
                                   (body.name or "").strip() or file_name)

    # ── תבניות: מחושב מחדש, לא סומכים על ה-preview ─────────────────────────
    decisions = _resolve(db, phone_id, sc)
    meta = ((sc.get("metadata") or {}).get("templates")) or {}
    id_map: dict[str, tuple] = {}

    for d in decisions:
        if d["action"] in ("matched_id", "matched_signature"):
            id_map[d["sourceId"]] = (d["targetId"], d["targetName"], d["targetLang"])
        elif d["action"] == "create":
            row = _create_template(db, phone_id, d["targetName"], d["targetLang"], meta[d["sourceId"]])
            d["targetId"] = row["id"]
            id_map[d["sourceId"]] = (row["id"], row["name"], row["lang"])
            d.update(await _try_publish(db, phone_id, row["id"]))

    # ── תרחיש ──────────────────────────────────────────────────────────────
    canvas = _rewrite_canvas(sc.get("canvas") or [], id_map)
    contact_id = _valid_contact(db, phone_id, sc.get("botContact"))
    bot_contact = sc.get("botContact") if contact_id else None
    config = _build_config((target or {}).get("config") or {}, sc, canvas, bot_contact)
    event_type = _event_type(sc)

    if target:
        sid = target["id"]
        db.table("scenarios").update({
            "name":       name,
            "status":     "draft",          # override מבטל פרסום
            "config":     config,
            "event_type": event_type,
            "contact_id": contact_id,
        }).eq("id", sid).eq("phone_id", phone_id).execute()
    else:
        sid = str(uuid.uuid4())
        payload = {
            "id":         sid,
            "phone_id":   phone_id,
            "name":       name,
            "status":     "draft",
            "config":     config,
            "event_type": event_type,
            "priority":   15,
        }
        if contact_id:
            payload["contact_id"] = contact_id
        db.table("scenarios").insert(payload).execute()

    res = (
        db.table("scenarios").select(_SELECT)
        .eq("id", sid).eq("phone_id", phone_id).limit(1).execute()
    )
    if not res.data:
        raise HTTPException(status_code=500, detail="Import failed — scenario not written")

    logger.info(f"[IO] import {body.mode} scenario={sid} name={name!r} phone={phone_id} "
                f"templates={[(d['action'], d.get('targetName')) for d in decisions]}")

    return {"mode": body.mode, "scenario": _expand_config(res.data[0]), "templates": decisions}
