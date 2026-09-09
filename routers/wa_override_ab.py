"""
wa_override_ab.py — בדיקת override ברמת טלפון

שני קולטים: /wa-test/hook/a  ו-  /wa-test/hook/b
כל אחד רושם בלוג עם התווית שלו, כך שרואים לאן Meta שלחה בפועל.

ENV:
  WA_ACCESS_TOKEN   טוקן Meta
  WA_VERIFY_TOKEN   ברירת מחדל test123
  WA_PUBLIC_BASE    למשל https://backend.grossman.bot
"""

import os
import json
import httpx
from fastapi import APIRouter, Query, Request, Response

from logging_config import get_logger

logger = get_logger("wa_ab")

router = APIRouter(prefix="/wa-test", tags=["wa-override-ab"])

GRAPH_BASE   = f"https://graph.facebook.com/{os.getenv('WA_GRAPH_VERSION', 'v25.0')}"
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "test123")
PUBLIC_BASE  = os.getenv("WA_PUBLIC_BASE", "https://backend.grossman.bot")


# ══════════════════════════════════════════════ שני הקולטים

@router.get("/hook/{slot}")
async def verify(
    slot: str,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """ההאנדשייק. Meta קוראת לזה ברגע שמגדירים את ה-override."""
    tag = slot.upper()
    logger.info(f"[{tag}] verify mode={hub_mode} token={hub_verify_token}")
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info(f"[{tag}] verify OK")
        return Response(content=hub_challenge or "", media_type="text/plain")
    logger.warning(f"[{tag}] verify FAILED")
    return Response(content="forbidden", status_code=403)


@router.post("/hook/{slot}")
async def receive(slot: str, request: Request):
    """מקבל הודעות וסטטוסים. רק לוג — לא מפעיל שום פייפליין."""
    tag = slot.upper()
    raw = await request.body()
    try:
        data = json.loads(raw)
    except Exception:
        logger.info(f"[{tag}] raw={raw.decode('utf-8', 'ignore')[:1000]}")
        return {"ok": True}

    for entry in data.get("entry", []) or []:
        for ch in entry.get("changes", []) or []:
            field = ch.get("field")
            v     = ch.get("value", {}) or {}
            pnid  = (v.get("metadata") or {}).get("phone_number_id")
            disp  = (v.get("metadata") or {}).get("display_phone_number")

            for m in v.get("messages", []) or []:
                mtype = m.get("type")
                extra = ""
                if mtype == "text":
                    extra = f" text={(m.get('text') or {}).get('body', '')[:60]!r}"
                elif mtype in ("image", "video", "audio", "document", "sticker"):
                    media = m.get(mtype) or {}
                    extra = f" media_id={media.get('id')} mime={media.get('mime_type')}"
                logger.info(
                    f"[{tag}] MSG field={field} pnid={pnid} ({disp}) "
                    f"from={m.get('from')} type={mtype} id={m.get('id')}{extra}"
                )

            for s in v.get("statuses", []) or []:
                logger.info(
                    f"[{tag}] STATUS pnid={pnid} ({disp}) id={s.get('id')} "
                    f"status={s.get('status')} to={s.get('recipient_id')}"
                )

            if not v.get("messages") and not v.get("statuses"):
                logger.info(f"[{tag}] OTHER field={field} value={json.dumps(v, ensure_ascii=False)[:500]}")

    return {"ok": True}


# ══════════════════════════════════════════════ החלפה בין A ל-B

async def _graph(method: str, pnid: str, **kw) -> dict:
    url = f"{GRAPH_BASE}/{pnid}"
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.request(
            method, url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            **kw,
        )
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:1000]}
    logger.info(f"[GRAPH] {method} {pnid} -> {r.status_code} {json.dumps(body, ensure_ascii=False)[:500]}")
    return {"ok": r.status_code < 400, "status": r.status_code, "body": body}


@router.get("/where/{pnid}")
async def where(pnid: str):
    """לאן המספר הזה שולח כרגע."""
    res = await _graph("GET", pnid, params={"fields": "webhook_configuration"})
    cfg = (res.get("body") or {}).get("webhook_configuration", {}) or {}
    phone = cfg.get("phone_number")
    waba  = cfg.get("whatsapp_business_account")
    app   = cfg.get("application")
    return {
        "phone_number_id": pnid,
        "level_phone": phone,
        "level_waba": waba,
        "level_app": app,
        "effective": phone or waba or app,
        "graph": res,
    }


@router.post("/switch/{pnid}/{slot}")
async def switch(pnid: str, slot: str):
    """
    slot = a | b | off
    off מחזיר את המספר ל-URL הקיים שלך (זה שברמת ה-app).
    """
    slot = slot.lower()
    if slot == "off":
        target = ""
        payload = {"webhook_configuration": {"override_callback_uri": ""}}
    elif slot in ("a", "b"):
        target = f"{PUBLIC_BASE}/wa-test/hook/{slot}"
        payload = {"webhook_configuration": {
            "override_callback_uri": target,
            "verify_token": VERIFY_TOKEN,
        }}
    else:
        return {"ok": False, "error": "slot must be a, b or off"}

    logger.info(f"[SWITCH] {pnid} -> {target or 'app default'}")
    res = await _graph("POST", pnid, json=payload)
    if not res.get("ok"):
        return res
    return await where(pnid)
