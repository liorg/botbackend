"""
Trusted domains — אימות URL והכנסה ל-trusted_domains.

אימות (הכול חייב לעבור):
  1. https בלבד, פורט 443, בלי user:pass, בלי IP
  2. דומיין תקין (IDNA → punycode), לא פנימי
  3. DNS → כל ה-IPs ציבוריים (is_global) — חוסם SSRF ל-10.x / 127.x / metadata
  4. TLS handshake מול ה-IP שנפתר: שרשרת + hostname תקינים, תוקף ≥ TRUSTED_CERT_MIN_DAYS

env (אופציונלי):
  TRUSTED_DOMAINS          רשימת בסיס גלובלית (אותה רשימה כמו ב-provisioner) — לבדיקת סקריפט
  TRUSTED_TLS_TIMEOUT_SEC  5
  TRUSTED_CERT_MIN_DAYS    7
  TRUSTED_MAX_PER_USER     20
"""
import asyncio
import ipaddress
import logging
import os
import re
import socket
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException

log = logging.getLogger("trusted_domains")

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")

TLS_TIMEOUT_SEC = float(os.getenv("TRUSTED_TLS_TIMEOUT_SEC", "5"))
CERT_MIN_DAYS = int(os.getenv("TRUSTED_CERT_MIN_DAYS", "7"))
MAX_PER_USER = int(os.getenv("TRUSTED_MAX_PER_USER", "20"))


class TrustedDomainError(ValueError):
    pass


# ── רשימות ────────────────────────────────────────────────────────────────

def parse_domains(raw) -> set:
    out = set()
    for part in (raw or "").split(","):
        d = part.strip().lower().rstrip(".")
        if d and DOMAIN_RE.match(d):
            out.add(d)
    return out


def base_domains() -> set:
    return parse_domains(os.getenv("TRUSTED_DOMAINS", ""))


def list_trusted(sb, user_id: str) -> list:
    res = sb.rpc("rpc_trusted_domain_list", {"p_user_id": user_id}).execute()
    return res.data if res.data is not None else []


def user_trusted_set(sb, user_id: str) -> set:
    rows = list_trusted(sb, user_id)
    return base_domains() | {r["domain"] for r in rows if r.get("status") == "verified"}


# ── אימות ─────────────────────────────────────────────────────────────────

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def host_from_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise TrustedDomainError("empty url")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise TrustedDomainError("malformed url")

    if parts.scheme.lower() != "https":
        raise TrustedDomainError("https only")
    if parts.username or parts.password:
        raise TrustedDomainError("credentials in url are not allowed")
    if port not in (None, 443):
        raise TrustedDomainError("only port 443 is allowed")

    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise TrustedDomainError("missing host")
    if _is_ip(host):
        raise TrustedDomainError("ip addresses are not allowed — use a domain")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise TrustedDomainError("invalid domain")
    if not DOMAIN_RE.match(host):
        raise TrustedDomainError("invalid domain")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise TrustedDomainError("internal domains are not allowed")
    return host


def _resolve_public(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise TrustedDomainError("dns lookup failed")
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise TrustedDomainError("dns returned no addresses")
    for ip in ips:
        if not ipaddress.ip_address(ip).is_global:
            raise TrustedDomainError(f"domain resolves to a non-public address ({ip})")
    return ips


def _check_tls(host: str, ip: str) -> dict:
    ctx = ssl.create_default_context()          # CERT_REQUIRED + check_hostname
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        # מתחברים ל-IP שכבר נבדק — לא נפתר שוב (DNS rebinding בין הבדיקה לחיבור)
        with socket.create_connection((ip, 443), timeout=TLS_TIMEOUT_SEC) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        raise TrustedDomainError(f"certificate invalid: {e.verify_message}")
    except ssl.SSLError as e:
        raise TrustedDomainError(f"tls handshake failed: {e.reason}")
    except (socket.timeout, OSError):
        raise TrustedDomainError("connection to port 443 failed")

    if not cert:
        raise TrustedDomainError("no certificate presented")

    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    if not_after - datetime.now(timezone.utc) < timedelta(days=CERT_MIN_DAYS):
        raise TrustedDomainError(f"certificate expires too soon ({not_after.date().isoformat()})")

    issuer = {k: v for rdn in cert.get("issuer", ()) for (k, v) in rdn}
    issuer_name = issuer.get("organizationName") or issuer.get("commonName")
    return {"cert_issuer": issuer_name, "cert_expires_at": not_after}


def _verify_sync(url: str) -> dict:
    host = host_from_url(url)
    ips = _resolve_public(host)
    tls = _check_tls(host, ips[0])
    return {"domain": host, "source_url": url.strip(), "resolved_ips": ips, **tls}


async def verify_url(url: str) -> dict:
    return await asyncio.to_thread(_verify_sync, url)


# ── בדיקת compile של סקריפט — זהה ל-ScriptUrlGuard.cs ────────────────────

SCRIPT_URL_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://"
    r"(?P<host>\$\{[^}]*\}|\[[^\]]*\]|[^/\s'\"`?#:\\)]+)"
    r"(?::(?P<port>[^/\s'\"`?#\\)]*))?"
)


def check_script_urls(code, trusted: set) -> list:
    """כל URL בקוד חייב להיות https לדומיין מהימן. מחזיר רשימת שגיאות (ריקה = תקין)."""
    errors, seen = [], set()
    for m in SCRIPT_URL_RE.finditer(code or ""):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        scheme = m.group("scheme").lower()
        host = m.group("host").lower().rstrip(".")
        port = m.group("port")

        if scheme != "https":
            errors.append(f"{raw} — https only")
        elif "@" in host or (port is not None and "@" in port):
            errors.append(f"{raw} — credentials in url are not allowed")
        elif host.startswith("${"):
            errors.append(f"{raw} — dynamic host is not allowed, write the domain literally")
        elif host.startswith("[") or _is_ip(host):
            errors.append(f"{raw} — ip addresses are not allowed")
        elif port is not None and port != "443":
            errors.append(f"{raw} — only port 443 is allowed")
        elif host not in trusted:
            errors.append(f"{raw} — domain '{host}' is not in trusted domains")
    return errors


def ensure_script_urls(sb, user_id: str, steps) -> None:
    """steps: [(step_id, code), ...] → 422 עם כל השגיאות לפני שמירה."""
    trusted = user_trusted_set(sb, user_id)
    items = []
    for step_id, code in steps:
        for err in check_script_urls(code, trusted):
            items.append({"step_id": step_id, "error": err})
    if items:
        raise HTTPException(422, detail={"error": "script_url_not_trusted", "items": items})


# ── כתיבה ─────────────────────────────────────────────────────────────────

def _upsert(sb, user_id: str, v: dict) -> dict:
    res = sb.rpc("rpc_trusted_domain_upsert", {
        "p_user_id": user_id,
        "p_domain": v["domain"],
        "p_source_url": v["source_url"],
        "p_cert_issuer": v["cert_issuer"],
        "p_cert_expires_at": v["cert_expires_at"].isoformat(),
        "p_resolved_ips": v["resolved_ips"],
    }).execute()
    return res.data


def _error_text(url: str, err: Exception) -> str:
    if isinstance(err, TrustedDomainError):
        return str(err)
    log.exception("trusted domain verify crashed | url=%s", url, exc_info=err)
    return "verification failed"


async def add_trusted_domain(sb, user_id: str, url: str) -> dict:
    """דומיין בודד — לא נוגע באחרים."""
    current = list_trusted(sb, user_id)
    try:
        v = await verify_url(url)
    except Exception as e:
        raise HTTPException(422, detail={"error": "trusted_domain_invalid",
                                         "items": [{"url": url, "error": _error_text(url, e)}]})
    if v["domain"] not in {r["domain"] for r in current} and len(current) >= MAX_PER_USER:
        raise HTTPException(422, detail={"error": "trusted_domain_limit", "max": MAX_PER_USER})
    return _upsert(sb, user_id, v)


async def sync_trusted_domains(sb, user_id: str, urls) -> list:
    """
    update_settings — רשימה מלאה. הכול או כלום:
    URL אחד שנכשל → 422, ושום דבר לא נכתב. מה שלא ברשימה → revoked.
    """
    urls = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    if len(urls) > MAX_PER_USER:
        raise HTTPException(422, detail={"error": "trusted_domain_limit", "max": MAX_PER_USER})

    results = await asyncio.gather(*(verify_url(u) for u in urls), return_exceptions=True)

    errors, verified = [], {}
    for url, res in zip(urls, results):
        if isinstance(res, Exception):
            errors.append({"url": url, "error": _error_text(url, res)})
        else:
            verified[res["domain"]] = res

    if errors:
        raise HTTPException(422, detail={"error": "trusted_domain_invalid", "items": errors})

    rows = [_upsert(sb, user_id, v) for v in verified.values()]
    sb.rpc("rpc_trusted_domain_revoke_missing", {
        "p_user_id": user_id,
        "p_keep": sorted(verified.keys()),
    }).execute()

    log.info("trusted domains synced | user=%s domains=%s", user_id, ",".join(sorted(verified)))
    return rows
