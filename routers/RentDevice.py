# routers/RentDevice.py
"""
RentDevice — טלפונים מושכרים (SIM פיזי ב-phone-hub) + טאב SMS.

    GET    /rent/phones                              זמינים + שלי (מדופדף, חיפוש)
    POST   /rent/phones/{phone_id}                   השכרה → ניקוי → user_id = שלי → seed templates
    DELETE /rent/phones/{phone_id}                   סיום ע"י הלקוח → ניקוי → system
    GET    /rent/phones/{phone_id}/sms/threads       טאב SMS: רשימה לפי שולח (מדופדף)
    GET    /rent/phones/{phone_id}/sms/thread        דיאלוג: keyset אחורה (before_at + before_id)

השכרה = טלפון חדש למשתמש: אותו כלל כמו provision — admin או 2FA.
פקיעה: לולאה ברקע כל RENT_SWEEP_SEC (rpc_rent_expire, skip locked — בטוח גם בכמה replicas).
כל ה-RPC רצים כ-service role; הבעלות נבדקת כאן.

רישום ב-main.py:
    from routers.RentDevice import router as rent_router
    app.include_router(rent_router, prefix="/api")
"""

import asyncio
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_supabase, get_current_user
from logging_config import get_logger

logger = get_logger("rent")

router = APIRouter(prefix="/rent", tags=["rent"])

RENT_SWEEP_SEC = int(os.getenv("RENT_SWEEP_SEC", "60"))

REASONS = {
    "not_found":     (404, "Phone not found"),
    "not_rentable":  (409, "This phone is not a rentable SIM"),
    "not_available": (409, "This phone was just rented by someone else"),
    "not_rented":    (409, "This phone is not rented"),
    "not_owner":     (403, "This rental belongs to another user"),
    "bad_user":      (400, "Invalid user"),
}


# ══════════════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════════════

class RentInfo(BaseModel):
    id: str
    start_time: str
    end_time: str


class RentPhone(BaseModel):
    phone_id: str
    number: str
    label: Optional[str] = None
    status: Optional[str] = None
    slot: Optional[int] = None
    carrier: Optional[str] = None
    wa_type: Optional[str] = Field(None, description="whatsapp / business / null = not installed")
    verified: Optional[bool] = None
    verified_at: Optional[str] = None
    device_status: Optional[str] = Field(None, description="add / remove (USB)")
    mine: bool = False
    available: bool = False
    rent: Optional[RentInfo] = None


class RentListResponse(BaseModel):
    items: list[RentPhone]
    total: int
    page: int
    page_size: int
    total_pages: int
    rent_hours: float = Field(..., description="Length of a new rental, bot_config rent.hours")


class RentStartResponse(BaseModel):
    ok: bool = True
    phone_id: str
    rent: RentInfo


class RentEndResponse(BaseModel):
    ok: bool = True
    phone_id: str


class SmsThreadItem(BaseModel):
    target_phone: Optional[str] = None
    last_body: Optional[str] = None
    last_at: Optional[str] = None
    count: int = 0


class SmsThreadsResponse(BaseModel):
    items: list[SmsThreadItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class SmsMessage(BaseModel):
    id: str
    msg_id: Optional[str] = None
    source_phone: Optional[str] = None
    target_phone: Optional[str] = None
    body: Optional[str] = None
    delivery_date: Optional[str] = None
    received_at: Optional[str] = None
    sort_at: str


class SmsThreadResponse(BaseModel):
    items: list[SmsMessage] = Field(..., description="Newest first")
    has_more: bool


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

def _cfg_num(db: Client, key: str, default: float) -> float:
    try:
        res = db.table("bot_config").select("value").eq("key", key).limit(1).execute()
        if res.data and str(res.data[0]["value"]).strip():
            return float(res.data[0]["value"])
    except Exception:
        pass
    return default


def _rpc(db: Client, fn: str, params: dict):
    return db.rpc(fn, params).execute().data


def _raise_reason(res: dict) -> None:
    code, msg = REASONS.get(res.get("reason"), (409, res.get("reason") or "failed"))
    raise HTTPException(status_code=code, detail=msg)


def _keep_templates() -> list[str]:
    """תבניות ההתקנה הראשונית — לא נמחקות בניקוי, ומשוחזרות אם חסרות."""
    from routers.template_manager import SEED_TEMPLATES
    return sorted({s["name"] for s in SEED_TEMPLATES})


async def _reseed(db: Client, phone_id: str) -> None:
    """אחרי ניקוי: seed שחסר → נוצר דרך ה-agent + approved/published."""
    from routers.phones import _get_host_for_phone, _ensure_seed_templates
    try:
        host = await _get_host_for_phone(db, phone_id)
        if host:
            await _ensure_seed_templates(db, phone_id, host)
        else:
            logger.warning(f"[RENT] reseed skipped — no host phone={phone_id}")
    except Exception as e:
        logger.warning(f"[RENT] reseed failed phone={phone_id}: {e}")


def _require_rent_allowed(db: Client, user: dict) -> None:
    from routers.phone_admin import is_phone_admin
    from routers.two_factor import TWO_FA_REQUIRED, is_2fa_verified
    if not (is_phone_admin(user) or is_2fa_verified(db, user["uid"])):
        raise HTTPException(status_code=403, detail=TWO_FA_REQUIRED)


def _require_my_rented_phone(db: Client, user: dict, phone_id: str) -> None:
    res = (
        db.table("phones").select("id")
        .eq("id", phone_id).eq("user_id", user["uid"])
        .not_.is_("sim_id", "null")
        .limit(1).execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Rented phone not found")


# ══════════════════════════════════════════════════════════════════════════
# Expiry sweep
# ══════════════════════════════════════════════════════════════════════════

async def _sweep_once(db: Client) -> None:
    expired = _rpc(db, "rpc_rent_expire", {"p_keep_templates": _keep_templates()}) or []
    for phone_id in expired:
        logger.info(f"[RENT] expired phone={phone_id}")
        await _reseed(db, phone_id)


async def _sweep_loop() -> None:
    db = get_supabase()
    while True:
        try:
            await _sweep_once(db)
        except Exception as e:
            logger.warning(f"[RENT] sweep failed: {e}")
        await asyncio.sleep(RENT_SWEEP_SEC)


@router.on_event("startup")
async def _start_sweep() -> None:
    asyncio.create_task(_sweep_loop())
    logger.info(f"[RENT] expiry sweep every {RENT_SWEEP_SEC}s")


# ══════════════════════════════════════════════════════════════════════════
# Rent
# ══════════════════════════════════════════════════════════════════════════

@router.get("/phones", response_model=RentListResponse, summary="List rentable phones",
            description="Available phones and the ones rented by the current user (mine first).")
def list_rent_phones(
    page: int = Query(1, ge=1),
    search: Optional[str] = Query(None, max_length=50),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    size = int(_cfg_num(db, "rent.paging", 10))
    res = _rpc(db, "rpc_rent_list", {
        "p_user_id": user["uid"], "p_page": page, "p_page_size": size, "p_search": search,
    }) or {}
    return {
        "items":       res.get("items") or [],
        "total":       res.get("total", 0),
        "page":        res.get("page", page),
        "page_size":   res.get("page_size", size),
        "total_pages": res.get("total_pages", 1),
        "rent_hours":  _cfg_num(db, "rent.hours", 1),
    }


@router.post("/phones/{phone_id}", response_model=RentStartResponse, summary="Rent a phone",
             description="Wipes the phone (keeps the seed templates), assigns it to the current user "
                         "for bot_config rent.hours, and re-seeds missing templates.")
async def start_rent(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    _require_rent_allowed(db, user)

    res = _rpc(db, "rpc_rent_start", {
        "p_phone_id": phone_id, "p_user_id": user["uid"], "p_hours": None,
        "p_keep_templates": _keep_templates(),
    }) or {}
    if not res.get("ok"):
        _raise_reason(res)

    logger.info(f"[RENT] start phone={phone_id} user={user['uid']} until={res['rent']['end_time']}")
    await _reseed(db, phone_id)
    return {"ok": True, "phone_id": phone_id, "rent": res["rent"]}


@router.delete("/phones/{phone_id}", response_model=RentEndResponse, summary="End my rental",
               description="Ends the current user's rental: wipes the phone and returns it to the pool.")
async def end_rent(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    res = _rpc(db, "rpc_rent_end", {
        "p_phone_id": phone_id, "p_user_id": user["uid"], "p_reason": "user",
        "p_keep_templates": _keep_templates(),
    }) or {}
    if not res.get("ok"):
        _raise_reason(res)

    logger.info(f"[RENT] end phone={phone_id} user={user['uid']}")
    await _reseed(db, phone_id)
    return {"ok": True, "phone_id": phone_id}


# ══════════════════════════════════════════════════════════════════════════
# SMS
# ══════════════════════════════════════════════════════════════════════════

@router.get("/phones/{phone_id}/sms/threads", response_model=SmsThreadsResponse,
            summary="SMS senders", description="SMS of a rented phone grouped by sender, newest first.")
def sms_threads(
    phone_id: str,
    page: int = Query(1, ge=1),
    search: Optional[str] = Query(None, max_length=50),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    _require_my_rented_phone(db, user, phone_id)
    size = int(_cfg_num(db, "sms.paging", 20))
    res = _rpc(db, "rpc_sms_threads", {
        "p_phone_id": phone_id, "p_page": page, "p_page_size": size, "p_search": search,
    }) or {}
    return {
        "items":       res.get("items") or [],
        "total":       res.get("total", 0),
        "page":        res.get("page", page),
        "page_size":   res.get("page_size", size),
        "total_pages": res.get("total_pages", 1),
    }


@router.get("/phones/{phone_id}/sms/thread", response_model=SmsThreadResponse,
            summary="SMS dialog", description="Messages of one sender, newest first. "
                                               "Pass before_at + before_id of the oldest shown to load older.")
def sms_thread(
    phone_id: str,
    target: str = Query("", description="Sender (target_phone). Empty = unknown sender"),
    before_at: Optional[datetime] = Query(None),
    before_id: Optional[str] = Query(None),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    _require_my_rented_phone(db, user, phone_id)
    res = _rpc(db, "rpc_sms_thread", {
        "p_phone_id":  phone_id,
        "p_target":    target or None,
        "p_before_at": before_at.isoformat() if before_at else None,
        "p_before_id": before_id,
        "p_limit":     int(_cfg_num(db, "sms.thread_limit", 30)),
    }) or {}
    return {"items": res.get("items") or [], "has_more": bool(res.get("has_more"))}
