"""
wa_override_ab.py — בדיקת override ברמת טלפון + **אימות ה-app secret על רטוב**

שני קולטים: /wa-test/hook/a  ו-  /wa-test/hook/b
כל אחד רושם בלוג עם התווית שלו, כך שרואים לאן Meta שלחה בפועל.

מה נוסף בגרסה הזו: כל POST מחשב HMAC-SHA256 על הגוף מול `WA_APP_SECRET`
ומשווה ל-`X-Hub-Signature-256` שמטא שלחה. **הוא לא דוחה** — רק רושם. ככה
אפשר לוודא שהסוד נכון בלי להסתכן באיבוד הודעות.

זה הדבר היחיד שמוכיח שה-app secret שלך הוא של האפליקציה שמחזיקה את המנוי.
whqueue דוחה ב-401 על סוד שגוי, וכאן רואים את זה לפני שזה קורה שם.

ENV:
  WA_ACCESS_TOKEN   טוקן Meta
  WA_APP_SECRET     App → Settings → Basic → App Secret   ← החדש
  WA_VERIFY_TOKEN   ברירת מחדל test123
  WA_PUBLIC_BASE    למשל https://backend.grossman.bot
"""

import hashlib
import hmac
import json
import os

import httpx

from fastapi import APIRouter, Query, Request, Response, HTTPException

from logging_config import get_logger

logger = get_logger("wa_ab")

router = APIRouter(prefix="/wa-test", tags=["wa-override-ab"])

GRAPH_BASE   = f"https://graph.facebook.com/{os.getenv('WA_GRAPH_VERSION', 'v25.0')}"
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "test123")
PUBLIC_BASE  = os.getenv("WA_PUBLIC_BASE", "https://backend.grossman.bot")
APP_SECRET   = os.getenv("WA_APP_SECRET", "")

# כמה POST-ים ראשונים מדפיסים את **כל** ההדרים. פעם אחת זה מלמד יותר מכל
# תיעוד: רואים בדיוק מה מטא שולחת, ומה היא לא (אין verify token, אין טוקן).
_dump_left = int(os.getenv("WA_DUMP_HEADERS", "3"))


# ══════════════════════════════════════════════ אימות החתימה

def check_signature(raw: bytes, header: str) -> dict:
    """
    מחזיר את הפירוט, לא רק true/false — כי כשזה נכשל רוצים לדעת למה.

    שים לב ש-raw הוא הבייטים **המדויקים** שהגיעו. כל re-serialize של ה-JSON
    (אפילו כזה שמייצר JSON תקין לגמרי) משנה את החתימה.
    """
    out = {"ok": False, "why": "", "bytes": len(raw)}

    if not APP_SECRET:
        out["why"] = "WA_APP_SECRET לא מוגדר"
        return out
    if not header:
        out["why"] = "אין הדר X-Hub-Signature-256 — זה לא webhook של מטא"
        return out
    if not header.startswith("sha256="):
        out["why"] = f"פורמט הדר לא צפוי: {header[:20]}"
        return out

    theirs = header.split("=", 1)[1].strip()
    mine   = hmac.new(APP_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    out["ok"] = hmac.compare_digest(mine, theirs)
    if not out["ok"]:
        # שמונה תווים מכל אחד מספיקים להשוואה ולא חושפים כלום — החתימה
        # ממילא פומבית, והסוד לא נגזר ממנה.
        out["why"] = f"לא תואם · שלהם={theirs[:8]}… שלי={mine[:8]}…"
    return out


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
    global _dump_left
    tag = slot.upper()
    raw = await request.body()

    # ── החתימה, לפני כל השאר ─────────────────────────────────────────────
    sig = check_signature(raw, request.headers.get("X-Hub-Signature-256", ""))
    if sig["ok"]:
        logger.info(f"[{tag}] SIG ✓ תואם · {sig['bytes']} בתים")
    else:
        logger.error(f"[{tag}] SIG ✗ {sig['why']} · {sig['bytes']} בתים")

    if _dump_left > 0:
        _dump_left -= 1
        hdrs = {k: v for k, v in request.headers.items()
                if k.lower() not in ("cookie", "authorization")}
        logger.info(f"[{tag}] HEADERS {json.dumps(hdrs, ensure_ascii=False)}")
        logger.info(f"[{tag}] QUERY   {dict(request.query_params)}")

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


@router.get("/sig-status")
async def sig_status(request: Request):
    want = os.getenv("WA_DEBUG_TOKEN", "")
    if not want or request.headers.get("X-Debug-Token") != want:
        raise HTTPException(status_code=404)      # 404 ולא 403 — לא מסגירים שהוא קיים
    """
    בלי להריץ webhook: האם הסוד בכלל טעון, ומה טביעת האצבע שלו.

    ה-sha הוא של הסוד ולא הסוד — אפשר להשוות אותו מול whqueue:
        sudo docker exec $CID sh -c \\
          'cat /run/secrets/whqueue_app_secret | tr -d "\\n\\r " | sha256sum'
    שונה = שני הרכיבים מחזיקים app secrets שונים, ואחד מהם ייכשל.
    """
    if not APP_SECRET:
        return {"loaded": False, "hint": "WA_APP_SECRET לא מוגדר"}
    
    return {
        "loaded": True,
        "len": len(APP_SECRET),
        "looks_like_app_secret": len(APP_SECRET) == 32
                                 and all(c in "0123456789abcdef" for c in APP_SECRET.lower()),
        "sha256": hashlib.sha256(APP_SECRET.encode()).hexdigest(),
    }


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
