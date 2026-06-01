# routers/compile_check.py
import os
import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Literal, Optional

compile_router = APIRouter(prefix="/scenarios", tags=["compile"])

DENO_URL     = os.getenv("DENO_COMPILE_URL", "http://localhost:8765")
DENO_TIMEOUT = 15.0


class CompileRequest(BaseModel):
    code:      str
    card_type: Literal["sender", "expect"] = "sender"


class CompileResponse(BaseModel):
    ok:          bool        = False
    errors:      list[str]   = []
    output:      Optional[dict] = None
    type_errors: list[str]   = []


@compile_router.post("/compile-check", response_model=CompileResponse)
async def compile_check(req: CompileRequest):
    # ── validations ──────────────────────────────────────────────────────────
    if not req.code or not req.code.strip():
        return CompileResponse(ok=False, errors=["code is required"])

    if len(req.code) > 50_000:
        return CompileResponse(ok=False, errors=["code too large (max 50KB)"])

    # ── call Deno ─────────────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=DENO_TIMEOUT) as client:
            resp = await client.post(
                f"{DENO_URL}/",
                json={"code": req.code, "card_type": req.card_type},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

    except httpx.ConnectError:
        # ✅ Deno לא רץ — לא מפיל את האפליקציה
        return CompileResponse(
            ok=False,
            errors=["שרת ה-Deno לא פעיל — compile-check אינו זמין כרגע"],
        )
    except httpx.TimeoutException:
        return CompileResponse(
            ok=False,
            errors=["timeout: הקוד רץ יותר מ-15 שניות"],
        )
    except httpx.HTTPStatusError as e:
        return CompileResponse(
            ok=False,
            errors=[f"Deno error: {e.response.text[:200]}"],
        )
    except Exception as e:
        return CompileResponse(
            ok=False,
            errors=[f"שגיאה לא צפויה: {str(e)}"],
        )

    return CompileResponse(
        ok=data.get("ok", False),
        errors=data.get("errors", []),
        output=data.get("output"),
        type_errors=data.get("type_errors", []),
    )