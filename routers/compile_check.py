# routers/compile_check.py — פרוקסי לכלי הפיתוח (DevToolsController) על ה-Worker הגנרי לבדיקות
# רשום כבר ב-main.py:
#   from routers.compile_check import compile_router
#   app.include_router(compile_router, prefix="/api")
#
# ה-Worker הגנרי (container ייעודי לבדיקות Deno/compile — לא worker של טלפון ספציפי,
# ראה docker-compose: service "worker" / "scenario-worker") חושף:
#   POST /dev/deno/test        — בדיקת קוד Deno בודד
#   POST /dev/scenario/compile — קומפילציית ICR מלאה
#
# כתובת קבועה (env), לא תלויה ב-phone_id/phone_workers — יש רק Worker אחד ייעודי לבדיקות.
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Any, Literal

from dependencies import get_current_user
from logging_config import get_logger

logger = get_logger("compile_check")

compile_router = APIRouter(prefix="/scenarios", tags=["scenario-dev"])

# ברירת מחדל: שם השירות ב-docker-compose (worker/scenario-worker) בפורט 9000.
# ב-Swarm/פרודקשן — לדרוס ל-service name / hostname האמיתי של ה-Worker הייעודי לבדיקות.
DEV_WORKER_URL = os.getenv("DEV_WORKER_URL", "http://scenario-worker:9000").rstrip("/")
WORKER_TIMEOUT = float(os.getenv("WORKER_DEV_TIMEOUT", "30"))


# ══════════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════════

class DenoTestPayload(BaseModel):
    scenarioId: Optional[str] = None
    stepId: Optional[str] = None
    contactId: Optional[str] = None
    contactPhone: Optional[str] = None
    contactName: Optional[str] = None
    lastMessageNum: Optional[str] = None
    lastMessageValue: Optional[str] = None
    variables: Optional[dict[str, str]] = None


class CompileCheckRequest(BaseModel):
    code: str
    card_type: Literal["expect", "sender"] = "sender"
    timeout_ms: Optional[int] = 5000
    payload: Optional[DenoTestPayload] = None


class ScenarioCompileRequest(BaseModel):
    scenario_json: str


# ══════════════════════════════════════════════════════════════════════
# Worker calls
# ══════════════════════════════════════════════════════════════════════

async def _post_worker(path: str, payload: dict) -> dict:
    url = f"{DEV_WORKER_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"[compile-check] worker {url} → {e.response.status_code}: {e.response.text[:300]}")
        raise HTTPException(status_code=502, detail=f"Worker error: {e.response.text[:300]}")
    except httpx.RequestError as e:
        logger.error(f"[compile-check] cannot reach dev worker at {url}: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot reach dev worker at {url}")


# ══════════════════════════════════════════════════════════════════════
# POST /scenarios/compile-check — בדיקת קוד Deno של כרטיס
# ══════════════════════════════════════════════════════════════════════

@compile_router.post("/compile-check")
async def compile_check(
    body: CompileCheckRequest,
    user=Depends(get_current_user),
):
    if not (body.code or "").strip():
        raise HTTPException(status_code=400, detail="code is required")

    worker_body: dict[str, Any] = {
        "code":       body.code,
        "card_type":  body.card_type,
        "timeout_ms": body.timeout_ms or 5000,
    }
    if body.payload:
        worker_body["payload"] = body.payload.model_dump(exclude_none=True)

    result = await _post_worker("/dev/deno/test", worker_body)

    # נרמול לצורה שה-UI (CodeEditor → CheckResultBadge) מצפה לה
    if bool(result.get("ok")):
        return {
            "ok":         True,
            "output":     result.get("value"),
            "proceed":    result.get("proceed", True),
            "output_var": result.get("output_var"),
            "stdout":     result.get("stdout"),
            "errors":     [],
        }

    detail = result.get("detail") or result.get("error") or "Unknown error"
    return {
        "ok":     False,
        "errors": [str(detail)],
        "stdout": result.get("stdout"),
    }


# ══════════════════════════════════════════════════════════════════════
# POST /scenarios/compile — קומפילציית ICR מלאה
# ══════════════════════════════════════════════════════════════════════

@compile_router.post("/compile")
async def compile_scenario(
    body: ScenarioCompileRequest,
    user=Depends(get_current_user),
):
    if not (body.scenario_json or "").strip():
        raise HTTPException(status_code=400, detail="scenario_json is required")

    result = await _post_worker("/dev/scenario/compile", {"scenario_json": body.scenario_json})

    if result.get("ok"):
        return {
            "ok":         True,
            "step_count": result.get("step_count"),
            "warnings":   result.get("warnings") or [],
            "scenario":   result.get("scenario"),
            "errors":     [],
        }

    return {
        "ok":       False,
        "errors":   [str(result.get("error") or "Compile failed")],
        "warnings": result.get("warnings") or [],
    }
