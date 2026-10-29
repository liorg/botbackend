# routers/regex_ai.py
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from typing import Optional

from services.regex_ai import generate_regex, health, RegexAIError
from routers.common_schemas import Passthrough

router = APIRouter(prefix="/ai/regex", tags=["regex-ai"])


class RegexRequest(BaseModel):
    request: str = Field(..., max_length=500, description="Free-text description of the condition (he/en/ru/ar).")
    samples: list[str] = Field(default_factory=list, max_length=20, description="Messages to test against the generated regex.")


class RegexTestRequest(BaseModel):
    regex: str = Field(..., max_length=2000, description="Regex to test. {{placeholders}} match any word.")
    samples: list[str] = Field(..., max_length=20, description="Messages to test.")


class RegexSampleResult(BaseModel):
    text: str = Field(..., description="The sample message.")
    match: bool = Field(..., description="True when the regex matches the sample.")


class RegexGenerateResponse(Passthrough):
    """generate_regex() output + tests when samples were sent. Other keys are kept."""
    regex: str = Field(..., description="Generated regex.")
    tests: Optional[list[RegexSampleResult]] = Field(None, description="Result per sample, when samples were sent.")


class RegexTestResponse(BaseModel):
    tests: list[RegexSampleResult] = Field(..., description="Result per sample.")


class RegexAIHealthResponse(Passthrough):
    """services.regex_ai.health() output."""


PLACEHOLDER_RE = re.compile(r"\{\{\s*[^{}]+?\s*\}\}")


def _test(pattern: str, samples: list[str]) -> list[dict]:
    # {{payload.x}} מתמלא רק בזמן ריצה — בבדיקה הוא "כל מילה"
    rx = re.compile(PLACEHOLDER_RE.sub(lambda _: r"\S+", pattern))
    return [{"text": s, "match": bool(rx.search(s))} for s in samples]


@router.post("/generate", response_model=RegexGenerateResponse, summary="Natural language → regex",
             description="Qwen3 breaks the description into JSON conditions, and a deterministic compiler builds the regex. When samples are sent, each is tested against it.")
async def generate(body: RegexRequest):
    try:
        res = await generate_regex(body.request)
    except RegexAIError as e:
        raise HTTPException(status_code=e.status, detail={"message": str(e), "raw": e.raw})
    if body.samples:
        res["tests"] = _test(res["regex"], body.samples)
    return res


@router.post("/test", response_model=RegexTestResponse, summary="Test a regex against samples", description="Tests a regex against sample messages. {{placeholders}} match any word. Returns 400 for an invalid regex.")
async def test(body: RegexTestRequest):
    try:
        return {"tests": _test(body.regex, body.samples)}
    except re.error as e:
        raise HTTPException(status_code=400, detail={"message": f"Invalid regex: {e}"})


@router.get("/health", response_model=RegexAIHealthResponse, summary="AI endpoint health", description="Checks that the regex AI endpoint (vLLM) is reachable.")
async def ai_health():
    return await health()
