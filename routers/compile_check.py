# routers/compile_check.py — פרוקסי לכלי הפיתוח של ה-Worker (DevToolsController)
# רשום כבר ב-main.py:
#   from routers.compile_check import compile_router
#   app.include_router(compile_router, prefix="/api")
#
# ה-Worker (.NET Core, Docker Swarm) חושף:
#   POST /dev/deno/test        — בדיקת קוד Deno בודד
#   POST /dev/scenario/compile — קומפילציית ICR מלאה
#
# כתובת ה-Worker נפתרת מ-phone_workers.service_name (DNS פנימי של Swarm) —
# בדיוק כמו שאר הקריאות מה-Spine ל-Worker. אין תלות ב-localhost.
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Any, Literal

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger

logger = get_logger("compile_check")

compile_router = APIRouter(prefix="/scenarios", tags=["scenario-dev"])

WORKER_PORT    = int(os.getenv("WORKER_PORT", "9000"))
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
    phone_id: str                              # חובה — כדי לאתר את ה-Worker הנכון
    timeout_ms: Optional[int] = 5000
    payload: Optional[DenoTestPayload] = None


class ScenarioCompileRequest(BaseModel):
    scenario_json: str
    phone_id: str


# ══════════════════════════════════════════════════════════════════════
# Worker resolution — אך ורק דרך phone_workers (כמו שאר המערכת מדברת ל-Worker)
# ══════════════════════════════════════════════════════════════════════

def _worker_base_url(db: Client, phone_id: str) -> str:
    """
    service_name לפי הקונבנציה שלכם: worker-{phone_number}-{phone_id[:8]}
    השם עצמו נשמר ב-phone_workers.service_name ומשמש כ-DNS פנימי בתוך ה-Swarm.
    """
    try:
        res = (
            db.table("phone_workers")
            .select("service_name, status")
            .eq("phone_id", str(phone_id))
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error(f"[compile-check] phone_workers lookup failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to resolve worker")

    if not res.data or not res.data[0].get("service_name"):
        raise HTTPException(status_code=404, detail=f"No worker registered for phone_id={phone_id}")

    row = res.data[0]
    if row.get("status") not in (None, "running", "active"):
        logger.warning(f"[compile-check] worker for phone_id={phone_id} status={row.get('status')}")

    return f"http://{row['service_name']}:{WORKER_PORT}"


async def _post_worker(base_url: str, path: str, payload: dict) -> dict:
    url = f"{base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"[compile-check] worker {url} → {e.response.status_code}: {e.response.text[:300]}")
        raise HTTPException(status_code=502, detail=f"Worker error: {e.response.text[:300]}")
    except httpx.RequestError as e:
        logger.error(f"[compile-check] cannot reach worker at {url}: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot reach worker at {url}")


# ══════════════════════════════════════════════════════════════════════
# POST /scenarios/compile-check — בדיקת קוד Deno של כרטיס
# ══════════════════════════════════════════════════════════════════════

@compile_router.post("/compile-check")
async def compile_check(
    body: CompileCheckRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    if not (body.code or "").strip():
        raise HTTPException(status_code=400, detail="code is required")

    base_url = _worker_base_url(db, body.phone_id)

    worker_body: dict[str, Any] = {
        "code":       body.code,
        "card_type":  body.card_type,
        "timeout_ms": body.timeout_ms or 5000,
    }
    if body.payload:
        worker_body["payload"] = body.payload.model_dump(exclude_none=True)

    result = await _post_worker(base_url, "/dev/deno/test", worker_body)

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
    db: Client = Depends(get_supabase),
):
    if not (body.scenario_json or "").strip():
        raise HTTPException(status_code=400, detail="scenario_json is required")

    base_url = _worker_base_url(db, body.phone_id)
    result = await _post_worker(base_url, "/dev/scenario/compile", {"scenario_json": body.scenario_json})

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
