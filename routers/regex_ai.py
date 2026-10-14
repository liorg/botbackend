# routers/regex_ai.py
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.regex_ai import generate_regex, health, RegexAIError

router = APIRouter(prefix="/ai/regex", tags=["regex-ai"])


class RegexRequest(BaseModel):
    request: str = Field(..., max_length=500, description="תיאור חופשי של התנאי (he/en/ru/ar).")
    samples: list[str] = Field(default_factory=list, max_length=20, description="הודעות לבדיקה מול ה-regex.")


class RegexTestRequest(BaseModel):
    regex: str = Field(..., max_length=2000)
    samples: list[str] = Field(..., max_length=20)


def _test(pattern: str, samples: list[str]) -> list[dict]:
    rx = re.compile(pattern)
    return [{"text": s, "match": bool(rx.search(s))} for s in samples]


@router.post("/generate", summary="Natural language → regex",
             description="Qwen3 מפרק את התיאור ל-JSON תנאים, והקומפיילר הדטרמיניסטי בונה regex.")
async def generate(body: RegexRequest):
    try:
        res = await generate_regex(body.request)
    except RegexAIError as e:
        raise HTTPException(status_code=e.status, detail={"message": str(e), "raw": e.raw})
    if body.samples:
        res["tests"] = _test(res["regex"], body.samples)
    return res


@router.post("/test", summary="Test a regex against samples")
async def test(body: RegexTestRequest):
    try:
        return {"tests": _test(body.regex, body.samples)}
    except re.error as e:
        raise HTTPException(status_code=400, detail={"message": f"Invalid regex: {e}"})


@router.get("/health", summary="AI endpoint health")
async def ai_health():
    return await health()
