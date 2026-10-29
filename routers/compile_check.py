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
from pydantic import BaseModel, Field
from typing import Optional, Any, Literal, Union

from dependencies import get_current_user, get_service_supabase
from services.trusted_domains import user_trusted_set, base_domains
from logging_config import get_logger

logger = get_logger("compile_check")

compile_router = APIRouter(prefix="/scenarios", tags=["scenario-dev"])

# ברירת מחדל: localhost:9000 — ה-Worker חשוף כ-127.0.0.1:9000 על אותו host שבו רץ ה-Spine.
# אם ה-Spine עצמו רץ בתוך container, יש לוודא שהוא על network=host, או לדרוס כאן.
DEV_WORKER_URL = os.getenv("DEV_WORKER_URL", "http://localhost:9000").rstrip("/")
WORKER_TIMEOUT = float(os.getenv("WORKER_DEV_TIMEOUT", "30"))


# ══════════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════════

class DenoTestPayload(BaseModel):
    scenarioId: Optional[str] = Field(None, description="Scenario id.")
    stepId: Optional[str] = Field(None, description="Step (card) id.")
    contactId: Optional[str] = Field(None, description="Contact id.")
    contactPhone: Optional[str] = Field(None, description="Contact phone number.")
    contactName: Optional[str] = Field(None, description="Contact name.")
    lastMessageNum: Optional[str] = Field(None, description="Number of the last message.")
    lastMessageValue: Optional[str] = Field(None, description="Text of the last message.")
    variables: Optional[dict[str, str]] = Field(None, description="Scenario variables available to the code.")


class CompileCheckRequest(BaseModel):
    code: str = Field(..., description="Deno code of the card.")
    card_type: Literal["expect", "sender"] = Field("sender", description="expect or sender.")
    timeout_ms: Optional[int] = Field(5000, description="Run timeout in milliseconds; default 5000.")
    payload: Optional[DenoTestPayload] = Field(None, description="Test context passed to the code.")


class ScenarioCompileRequest(BaseModel):
    scenario_json: str = Field(..., description="Full scenario as a JSON string.")


# ── Response Schemas ──────────────────────────────────────────────────────

class CompileCheckOk(BaseModel):
    ok: Literal[True] = Field(True, description="Always true.")
    output: Any = Field(None, description="Value returned by the code.")
    proceed: Any = Field(True, description="Whether the flow continues after this card.")
    output_var: Optional[str] = Field(None, description="Variable the output is stored in.")
    stdout: Optional[str] = Field(None, description="Console output of the run.")
    errors: list[str] = Field([], description="Always empty.")


class CompileCheckFailed(BaseModel):
    ok: Literal[False] = Field(False, description="Always false.")
    errors: list[str] = Field(..., description="Error messages.")
    stdout: Optional[str] = Field(None, description="Console output of the run.")


class ScenarioCompileOk(BaseModel):
    ok: Literal[True] = Field(True, description="Always true.")
    step_count: Optional[int] = Field(None, description="Number of compiled steps.")
    warnings: list[Any] = Field([], description="Compiler warnings.")
    scenario: Any = Field(None, description="Compiled ICR scenario.")
    errors: list[str] = Field([], description="Always empty.")


class ScenarioCompileFailed(BaseModel):
    ok: Literal[False] = Field(False, description="Always false.")
    errors: list[str] = Field(..., description="Error messages.")
    warnings: list[Any] = Field([], description="Compiler warnings.")


# ══════════════════════════════════════════════════════════════════════
# Worker calls
# ══════════════════════════════════════════════════════════════════════

def _post_worker_sync(path: str, payload: dict) -> dict:
    """גרסה סינכרונית — בלי event loop. אותו טיפול בשגיאות כמו _post_worker."""
    url = f"{DEV_WORKER_URL}{path}"
    try:
        with httpx.Client(timeout=WORKER_TIMEOUT) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"[compile-check] worker {url} → {e.response.status_code}: {e.response.text[:300]}")
        raise HTTPException(status_code=502, detail=f"Worker error: {e.response.text[:300]}")
    except httpx.RequestError as e:
        logger.error(f"[compile-check] cannot reach dev worker at {url}: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot reach dev worker at {url}")


# נשאר לתאימות לאחור אם משהו אחר עדיין עושה await
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


def _trusted_for_user(user) -> list:
    """ה-dev worker משותף לכל המשתמשים — הרשימה נשלחת בכל בקשה, לא מ-env."""
    try:
        return sorted(user_trusted_set(get_service_supabase(), user["uid"]))
    except Exception as e:  # noqa: BLE001
        logger.error(f"[compile-check] trusted domains lookup failed: {e}")
        return sorted(base_domains())


# ══════════════════════════════════════════════════════════════════════
# POST /scenarios/compile-check — בדיקת קוד Deno של כרטיס
# ══════════════════════════════════════════════════════════════════════

@compile_router.post("/compile-check", response_model=Union[CompileCheckOk, CompileCheckFailed], summary="Check card Deno code", description="Runs the card's Deno code on the dev Worker with the user's trusted domains. Returns { ok, output, ... } or { ok: false, errors }.")
def compile_check(
    body: CompileCheckRequest,
    user=Depends(get_current_user),
):
    if not (body.code or "").strip():
        raise HTTPException(status_code=400, detail="code is required")

    worker_body: dict[str, Any] = {
        "code":       body.code,
        "card_type":  body.card_type,
        "timeout_ms": body.timeout_ms or 5000,
        "trusted_domains": _trusted_for_user(user),
    }
    if body.payload:
        worker_body["payload"] = body.payload.model_dump(exclude_none=True)

    result = _post_worker_sync("/dev/deno/test", worker_body)

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

@compile_router.post("/compile", response_model=Union[ScenarioCompileOk, ScenarioCompileFailed], summary="Compile scenario", description="Compiles the full scenario to ICR on the dev Worker. Returns { ok, step_count, warnings, scenario } or { ok: false, errors, warnings }.")
def compile_scenario(
    body: ScenarioCompileRequest,
    user=Depends(get_current_user),
):
    if not (body.scenario_json or "").strip():
        raise HTTPException(status_code=400, detail="scenario_json is required")

    result = _post_worker_sync("/dev/scenario/compile", {
        "scenario_json":   body.scenario_json,
        "trusted_domains": _trusted_for_user(user),
    })

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