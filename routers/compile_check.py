# routers/compile_check.py
# FastAPI router — מקבל קוד TypeScript מה-frontend, מעביר ל-Deno לבדיקה
# mount ב-main.py: app.include_router(compile_router, prefix="/api")

import os
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal, Optional

compile_router = APIRouter(prefix="/scenarios", tags=["compile"])

DENO_URL = os.getenv("DENO_COMPILE_URL", "http://localhost:8765")
DENO_TIMEOUT = 15.0  # seconds


class CompileRequest(BaseModel):
    code: str
    card_type: Literal["sender", "expect"] = "sender"


class CompileResponse(BaseModel):
    ok: bool
    errors: list[str] = []
    output: Optional[dict] = None
    type_errors: list[str] = []


@compile_router.post("/compile-check", response_model=CompileResponse)
async def compile_check(req: CompileRequest):
    """
    מקבל קוד TypeScript מה-designer, שולח ל-Deno לבדיקת סוגים + ריצת test,
    מחזיר { ok, errors, output, type_errors }
    """
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="code is required")

    if len(req.code) > 50_000:
        raise HTTPException(status_code=400, detail="code too large (max 50KB)")

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
        raise HTTPException(
            status_code=503,
            detail="Deno compile server is not running. Start with: deno run --allow-net --allow-read --allow-write --allow-env deno_compile_server.ts"
        )
    except httpx.TimeoutException:
        return CompileResponse(
            ok=False,
            errors=["timeout: הקוד רץ יותר מ-15 שניות"],
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Deno error: {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return CompileResponse(
        ok=data.get("ok", False),
        errors=data.get("errors", []),
        output=data.get("output"),
        type_errors=data.get("type_errors", []),
    )