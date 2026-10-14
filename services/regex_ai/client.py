# services/regex_ai/client.py
# קריאה ל-vLLM (Qwen3-4B-AWQ) מאחורי Cloudflare Access.
#
# Cache בשתי רמות:
#  1. System prompt — prefix caching של vLLM (מופעל כברירת מחדל ב-V1, או
#     --enable-prefix-caching). התנאי: RULES זהה בכל קריאה, ותמיד ראשון.
#     אין כאן שום דבר דינמי לפני הודעת ה-user.
#  2. תוצאה — LRU בזיכרון לפי טקסט הבקשה המנורמל. temperature=0 → דטרמיניסטי.
import json
import os
import re
import time
from collections import OrderedDict

import httpx
from jsonschema import validate, ValidationError

from .rules import RULES
from .compiler import SCHEMA, normalize_dsl, compile_condition

AI_BASE_URL   = os.getenv("REGEX_AI_BASE_URL", "https://ai-dev.grossmanbot.download/v1")
AI_MODEL      = os.getenv("REGEX_AI_MODEL", "Qwen/Qwen3-4B-AWQ")
CF_ID         = os.getenv("CF_ACCESS_CLIENT_ID", "")
CF_SECRET     = os.getenv("CF_ACCESS_CLIENT_SECRET", "")
AI_TIMEOUT    = float(os.getenv("REGEX_AI_TIMEOUT", "30"))
MAX_TOKENS    = int(os.getenv("REGEX_AI_MAX_TOKENS", "400"))
CACHE_SIZE    = int(os.getenv("REGEX_AI_CACHE_SIZE", "500"))
MAX_REQ_CHARS = 500


class RegexAIError(Exception):
    def __init__(self, message: str, status: int = 502, raw: str | None = None):
        super().__init__(message)
        self.status = status
        self.raw = raw


_client: httpx.AsyncClient | None = None
_cache: "OrderedDict[str, dict]" = OrderedDict()


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        headers = {"Content-Type": "application/json"}
        if CF_ID and CF_SECRET:
            headers["CF-Access-Client-Id"] = CF_ID
            headers["CF-Access-Client-Secret"] = CF_SECRET
        _client = httpx.AsyncClient(base_url=AI_BASE_URL, headers=headers, timeout=AI_TIMEOUT)
    return _client


async def close():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _key(request: str) -> str:
    return re.sub(r"\s+", " ", request).strip()


# ── Placeholders: {{payload.x}} ──────────────────────────────────────────────
# ערך שה-Worker ממלא בזמן ריצה. המודל לא רואה אותו — לפני השליחה הוא מוחלף בטוקן
# גנרי (PARAMA, PARAMB… — אותיות בלבד, כדי שלא ייקרא כספרות), ואחרי הקומפילציה
# הטוקן חוזר להיות {{payload.x}} בתוך ה-regex. ה-Worker מציב את הערך (עם Regex.Escape).
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _param_token(i: int) -> str:
    return "PARAM" + "".join(chr(65 + d) for d in _base26(i))


def _base26(i: int) -> list[int]:
    out = []
    while True:
        out.append(i % 26)
        i = i // 26 - 1
        if i < 0:
            return out[::-1]


def mask_placeholders(request: str) -> tuple[str, dict[str, str]]:
    """{{payload.x}} → "PARAMA". עוטף במרכאות אם המשתמש לא עטף — ערך קבוע תמיד במרכאות."""
    params: dict[str, str] = {}
    by_path: dict[str, str] = {}
    out, last = [], 0
    for m in PLACEHOLDER_RE.finditer(request):
        path = m.group(1).strip()
        token = by_path.get(path)
        if token is None:
            token = _param_token(len(by_path))
            by_path[path] = token
            params[token] = "{{" + path + "}}"
        quoted = m.start() > 0 and request[m.start() - 1] == '"' and request[m.end():m.end() + 1] == '"'
        out.append(request[last:m.start()])
        out.append(token if quoted else f'"{token}"')
        last = m.end()
    out.append(request[last:])
    return "".join(out), params


def unmask(text, params: dict[str, str]):
    if not params:
        return text
    if isinstance(text, str):
        for token in sorted(params, key=len, reverse=True):   # PARAMAB לפני PARAMA
            text = text.replace(token, params[token])
        return text
    if isinstance(text, list):
        return [unmask(t, params) for t in text]
    if isinstance(text, dict):
        return {k: unmask(v, params) for k, v in text.items()}
    return text


def _extract_json(raw: str) -> str:
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end > start else raw


async def _ask_model(request: str) -> dict:
    body = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": RULES},      # קבוע → prefix cache
            {"role": "user", "content": request},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        # אכיפת סכמה בצד vLLM — מקביל ל-lm-format-enforcer ב-POC
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "conditions", "schema": SCHEMA},
        },
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        r = await _http().post("/chat/completions", json=body)
    except httpx.HTTPError as e:
        raise RegexAIError(f"AI unreachable: {e}", 503)

    if r.status_code in (401, 403):
        raise RegexAIError("Cloudflare Access rejected the request (CF_ACCESS_CLIENT_ID/SECRET)", 502)
    if r.status_code == 400 and "maximum context length" in r.text:
        raise RegexAIError("max_model_len too small for RULES + request", 502, r.text[:500])
    if r.status_code != 200:
        raise RegexAIError(f"AI returned {r.status_code}", 502, r.text[:500])

    raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
    try:
        data = json.loads(_extract_json(raw))
        validate(data, SCHEMA)
        return data
    except json.JSONDecodeError:
        raise RegexAIError("Model returned invalid JSON", 422, raw)
    except ValidationError as e:
        raise RegexAIError(f"Schema violation: {e.message}", 422, raw)


async def generate_regex(request: str) -> dict:
    """תיאור חופשי → {regex, conditions, whole_input, cached, ms}."""
    key = _key(request)
    if not key:
        raise RegexAIError("Empty request", 400)
    if len(key) > MAX_REQ_CHARS:
        raise RegexAIError(f"Request longer than {MAX_REQ_CHARS} chars", 400)

    if key in _cache:
        _cache.move_to_end(key)
        return {**_cache[key], "cached": True, "ms": 0}

    t0 = time.perf_counter()
    masked, params = mask_placeholders(key)
    dsl = normalize_dsl(await _ask_model(masked), masked)
    try:
        pattern = compile_condition(dsl)
        re.compile(pattern)          # עם הטוקנים — {{…}} עדיין לא בפנים
    except (ValueError, re.error) as e:
        raise RegexAIError(f"Compile failed: {e}", 422, json.dumps(dsl, ensure_ascii=False))

    missing = [t for t in params if t not in pattern]
    if missing:
        raise RegexAIError(
            "Placeholder lost: " + ", ".join(params[t] for t in missing), 422,
            json.dumps(dsl, ensure_ascii=False))

    result = {
        "regex":       unmask(pattern, params),
        "conditions":  unmask(dsl["conditions"], params),
        "whole_input": dsl["whole_input"],
        "params":      sorted(set(params.values())),   # מה ה-Worker צריך למלא
    }
    _cache[key] = result
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return {**result, "cached": False, "ms": int((time.perf_counter() - t0) * 1000)}


async def health() -> dict:
    try:
        r = await _http().get("/models")
        models = [m["id"] for m in r.json().get("data", [])] if r.status_code == 200 else []
        max_len = next((m.get("max_model_len") for m in r.json().get("data", []) if m["id"] == AI_MODEL), None) if models else None
        return {"ok": AI_MODEL in models, "status": r.status_code, "models": models,
                "max_model_len": max_len, "cache_entries": len(_cache)}
    except Exception as e:
        return {"ok": False, "error": str(e), "cache_entries": len(_cache)}
