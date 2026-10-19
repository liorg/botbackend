"""
routers/two_factor.py — 2FA.

    POST /settings/2fa/request   writes a row to otp_requests; PhoneOrchestrator sends it.
                                 waits up to 8s for the result: 200 sent | 202 queued | 502 failed
    POST /settings/2fa/verify    {"code": "123456"}
    GET  /settings/2fa/status

Gate:
    from routers.two_factor import require_2fa
    user=Depends(require_2fa)
"""
import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from dependencies import get_current_user, get_supabase
from services import otp

router = APIRouter(prefix="/settings/2fa", tags=["2fa"])

TWO_FA_REQUIRED = "TWO_FACTOR_REQUIRED"
SEND_WAIT_SEC = 8

_ERRORS = {
    "NO_USER":          (404, "User not found"),
    "NO_MOBILE":        (400, "Add a mobile number first"),
    "ALREADY_VERIFIED": (409, "Mobile is already verified"),
    "RESEND_TOO_SOON":  (429, "Please wait {retry_in}s before requesting a new code"),
    "RATE_LIMITED":     (429, "Too many codes. Try again in {retry_in}s"),
    "NO_ACTIVE":        (404, "No active code. Request a new one"),
    "EXPIRED":          (410, "The code has expired. Request a new one"),
    "TOO_MANY":         (429, "Too many attempts. Request a new code"),
    "MOBILE_CHANGED":   (409, "The mobile number changed. Request a new code"),
    "INVALID":          (400, "Wrong code ({remaining} attempts left)"),
}


def _raise(r: dict):
    status, msg = _ERRORS.get(r.get("code"), (400, r.get("code") or "ERROR"))
    raise HTTPException(status, msg.format(retry_in=r.get("retry_in", ""), remaining=r.get("remaining", 0)))


def _uid(user) -> str:
    uid = getattr(user, "id", None)
    if uid is None and isinstance(user, dict):
        uid = user.get("id") or user.get("sub")
    if not uid:
        raise HTTPException(401, "Unauthorized")
    return str(uid)


def _rpc(db, name: str, params: dict) -> dict:
    data = db.rpc(name, params).execute().data
    return (data[0] if data else {}) if isinstance(data, list) else (data or {})


# ── gate ──────────────────────────────────────────────────────────────────
def is_2fa_verified(db, user_id: str) -> bool:
    row = db.table("users").select("two_factor_verified").eq("id", user_id).maybe_single().execute()
    return bool(((row and row.data) or {}).get("two_factor_verified"))


def require_2fa(user=Depends(get_current_user)):
    if not is_2fa_verified(get_supabase(), _uid(user)):
        raise HTTPException(403, TWO_FA_REQUIRED)
    return user


# ── endpoints ─────────────────────────────────────────────────────────────
@router.get("/status")
async def status(user=Depends(get_current_user)):
    row = (get_supabase().table("users").select("mobile, two_factor_verified, two_factor_verified_at")
           .eq("id", _uid(user)).maybe_single().execute())
    u = (row and row.data) or {}
    return {
        "verified":    bool(u.get("two_factor_verified")),
        "verified_at": u.get("two_factor_verified_at"),
        "mobile_set":  bool((u.get("mobile") or "").strip()),
        "ttl_sec":     otp.TTL_SEC,
        "resend_sec":  otp.RESEND_SEC,
    }


@router.post("/request")
async def request_otp(response: Response, user=Depends(get_current_user)):
    db, uid = get_supabase(), _uid(user)
    code = otp.new_code()

    r = _rpc(db, "rpc_otp_request", {
        "p_user_id": uid, "p_code": code, "p_code_hash": otp.hash_code(uid, code),
        "p_ttl_sec": otp.TTL_SEC, "p_resend_sec": otp.RESEND_SEC,
        "p_max_per_hour": otp.MAX_PER_HOUR, "p_max_attempts": otp.MAX_ATTEMPTS,
    })
    if not r.get("ok"):
        _raise(r)

    # PhoneOrchestrator takes it within ~2s — waiting for the result
    st, deadline = None, time.monotonic() + SEND_WAIT_SEC
    while time.monotonic() < deadline:
        res = db.table("otp_requests").select("send_status").eq("id", r["id"]).maybe_single().execute()
        st = ((res and res.data) or {}).get("send_status")
        if st in ("sent", "failed"):
            break
        await asyncio.sleep(0.5)

    if st == "failed":
        raise HTTPException(502, "Could not send the code over WhatsApp. Try again shortly")

    if st != "sent":
        response.status_code = 202
    return {
        "ok": True, "status": "sent" if st == "sent" else "queued",
        "sent_to": otp.mask(r["mobile"]), "expires_in": otp.TTL_SEC, "resend_in": otp.RESEND_SEC,
    }


class VerifyReq(BaseModel):
    code: str = Field(..., pattern=r"^\d{6}$")


@router.post("/verify")
async def verify_otp(req: VerifyReq, user=Depends(get_current_user)):
    db, uid = get_supabase(), _uid(user)
    r = _rpc(db, "rpc_otp_verify", {"p_user_id": uid, "p_code_hash": otp.hash_code(uid, req.code)})
    if not r.get("ok"):
        _raise(r)
    return {"ok": True, "verified": True, "verified_at": r.get("verified_at")}
