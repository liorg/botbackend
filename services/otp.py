"""
services/otp.py — code generation and HMAC. Sending is in PhoneOrchestrator (OtpSender).

env:
    OTP_PEPPER         openssl rand -hex 32
    OTP_TTL_SEC        420   (7 minutes)
    OTP_RESEND_SEC     60
    OTP_MAX_ATTEMPTS   5
    OTP_MAX_PER_HOUR   5
"""
import hashlib
import hmac
import os
import secrets

TTL_SEC      = int(os.getenv("OTP_TTL_SEC", "420"))
RESEND_SEC   = int(os.getenv("OTP_RESEND_SEC", "60"))
MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
MAX_PER_HOUR = int(os.getenv("OTP_MAX_PER_HOUR", "5"))
PEPPER       = os.getenv("OTP_PEPPER", "")


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(user_id: str, code: str) -> str:
    if not PEPPER:
        raise RuntimeError("OTP_PEPPER is not configured")
    return hmac.new(PEPPER.encode(), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def mask(mobile: str) -> str:
    return f"+{mobile[:3]} •••• {mobile[-4:]}" if len(mobile) >= 7 else f"+{mobile}"
