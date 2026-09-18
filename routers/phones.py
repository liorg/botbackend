"""
phones.py — FastAPI router
Proxy between UI and .NET Agent (WhatsAppDockerManager)
"""

import os
import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timedelta, timezone

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger
from config import internal_route,client_route,APP_MODE 
logger = get_logger("phones")

router = APIRouter(prefix="/phones", tags=["phones"])

AGENT_PORT    = int(os.getenv("AGENT_PORT", "5000"))
AGENT_TOKEN   = os.getenv("AGENT_TOKEN", "")
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "10"))
HOST_HEARTBEAT_TIMEOUT_MINUTES = int(os.getenv("HOST_HEARTBEAT_TIMEOUT", "60"))
BLOCKED_IPS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


class SendTextRequest(BaseModel):
    # The agent expects "jid". "to" is accepted as an alias so an older
    # caller keeps working; exactly one of them must be present.
    jid: Optional[str] = Field(None, description="Recipient WhatsApp JID, for example 972501234567@s.whatsapp.net.")
    to: Optional[str] = Field(None, description="Legacy alias for jid, used only when jid is empty.")
    text: str = Field(..., description="Message text.")

    @property
    def target(self) -> Optional[str]:
        return self.jid or self.to


class ProvisionRequest(BaseModel):
    phone_number: str = Field(..., description="Number to provision. Non-digits are removed; at least 7 digits.")
    nickname: Optional[str] = Field(None, description="Nickname passed to the agent.")
    tag: Optional[str] = Field(None, description="Tag passed to the agent.")
    use_pairing_code: Optional[bool] = Field(None, description="Link with a pairing code instead of a QR code.")  # 22


def _agent_headers() -> dict:
    return {"X-Agent-Token": AGENT_TOKEN, "Content-Type": "application/json"}


async def _agent_get(ip: str, path: str) -> dict:
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
        resp = await client.get(url, headers=_agent_headers())
        resp.raise_for_status()
        return resp.json()


async def _agent_post(ip: str, path: str, body: dict, timeout: float = None) -> dict:
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=timeout or AGENT_TIMEOUT) as client:
        resp = await client.post(url, headers=_agent_headers(), json=body)
        resp.raise_for_status()
        return resp.json()


async def _agent_post_with_retry(
    ip: str, path: str, body: dict,
    retries: int = 3, delay: float = 2.0, timeout: float = None,
) -> dict:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return await _agent_post(ip, path, body, timeout=timeout)
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            last_error = e
            logger.warning(
                f"[AGENT] Attempt {attempt}/{retries} failed for {ip}{path}: {e}"
            )
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
    raise last_error


def _is_valid_agent_ip(ip: str) -> bool:
    if not ip:
        return False
    ip_stripped = ip.strip().lower()
    if ip_stripped in BLOCKED_IPS:
        logger.warning(f"Skipping blocked IP: {ip}")
        return False
    if ip_stripped.startswith("127."):
        logger.warning(f"Skipping loopback IP: {ip}")
        return False
    return True


async def _get_active_hosts(db: Client) -> list[dict]:
    result = (
        db.table("agent_hosts")
        .select("id, host_name, ip_address, external_ip, max_containers, last_heartbeat")
        .eq("status", "active")
        .execute()
    )
    all_hosts = result.data or []
    valid = [h for h in all_hosts if _is_valid_agent_ip(h.get("ip_address", ""))]
    if len(all_hosts) != len(valid):
        logger.info(f"Skipped {len(all_hosts) - len(valid)} loopback/dev host(s)")
    return valid


async def _check_host_health(ip: str, db: Client = None, host_id: str = None) -> bool:
    try:
        data = await _agent_get(ip, "/api/host/health")
        is_healthy = data.get("status") == "healthy"
        if is_healthy and db and host_id:
            try:
                db.table("agent_hosts").update({
                    "last_heartbeat": datetime.now(timezone.utc).isoformat()
                }).eq("id", host_id).execute()
            except Exception:
                pass
        return is_healthy
    except Exception as e:
        logger.warning(f"Health check failed for {ip}: {e}")
        return False


async def _find_healthy_host(db: Client, retries: int = 3) -> Optional[dict]:
    for attempt in range(1, retries + 1):
        hosts = await _get_active_hosts(db)
        if not hosts:
            logger.error("No production agent hosts found")
            return None

        for host in hosts:
            ip = host.get("ip_address", "")
            if await _check_host_health(ip, db, host["id"]):
                logger.info(f"[AGENT] Found healthy host {host['host_name']} ({ip})")
                return host
            logger.warning(f"[AGENT] Host {host['host_name']} ({ip}) failed health check")

        if attempt < retries:
            logger.warning(f"[AGENT] No healthy host — retry {attempt}/{retries} in 3s")
            await asyncio.sleep(3)

    logger.error("[AGENT] All hosts failed health check after retries")
    return None

async def _agent_delete(ip: str, path: str, timeout: float = None) -> dict:
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=timeout or AGENT_TIMEOUT) as client:
        resp = await client.delete(url, headers=_agent_headers())
        resp.raise_for_status()
        return resp.json() if resp.content else {}

async def _get_host_for_phone(db: Client, phone_id: str) -> Optional[dict]:
    phone_res = db.table("phones").select("host_id").eq("id", phone_id).execute()
    if not phone_res.data:
        return None
    host_id = phone_res.data[0].get("host_id")
    if not host_id:
        return None
    host_res = db.table("agent_hosts").select("id, host_name, ip_address").eq("id", host_id).execute()
    if not host_res.data:
        return None
    host = host_res.data[0]
    if not _is_valid_agent_ip(host.get("ip_address", "")):
        logger.error(f"Phone {phone_id} assigned to loopback host — refusing")
        return None
    return host


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

# Columns safe to return to a browser. creds_base64 holds the Baileys session
# and must never leave the backend — never replace this with select("*").
PHONE_COLUMNS = (
    "id, user_id, number, label, color, status, docker_status, "
    "created_at, provider, lang, pairing_code, pairing_code_expiry, "
    "use_pairing_code"
)

@router.get("/", summary="List my phones", description="Returns the phones owned by the current user. Session credentials are never included.")
async def list_phones(user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    result = (
        db.table("phones")
        .select(PHONE_COLUMNS)
        .eq("user_id", user["uid"])
        .execute()
    )
    return result.data

@internal_route(router.get("/agents/health", summary="Check agent hosts health", description="Internal. Health-checks every active agent host, refreshes last_heartbeat on healthy ones and returns a summary."))
async def agents_health(user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    hosts = await _get_active_hosts(db)
    results = []
    for host in hosts:
        ip = host.get("ip_address", "")
        healthy = await _check_host_health(ip, db, host["id"])
        results.append({
            "host_id":        host["id"],
            "host_name":      host["host_name"],
            "ip_address":     ip,
            "last_heartbeat": host.get("last_heartbeat"),
            "healthy":        healthy,
        })
    return {
        "total":   len(results),
        "healthy": sum(1 for r in results if r["healthy"]),
        "hosts":   results,
    }

def _agent_template_payload(spec: dict) -> dict:
    """Map a seed spec to what TemplatesController expects.

    The controller reads `name` + `language`, and ToTemplateContent parses a
    WhatsApp-style `components` array — not our nested `content` object.
    The whole body is forwarded to the baileys container as-is.
    """
    c = spec["content"] or {}
    components: list[dict] = []

    header = c.get("header") or {}
    if header.get("text") and (header.get("format") or "none") != "none":
        components.append({
            "type":   "HEADER",
            "format": (header.get("format") or "text").upper(),
            "text":   header["text"],
        })

    body = c.get("body") or {}
    if body.get("text"):
        components.append({"type": "BODY", "text": body["text"]})

    footer = c.get("footer") or {}
    if footer.get("text"):
        components.append({"type": "FOOTER", "text": footer["text"]})

    buttons = c.get("buttons") or []
    if buttons:
        components.append({
            "type": "BUTTONS",
            "buttons": [
                {"type": (b.get("type") or "quick_reply").upper(), "text": b.get("text")}
                for b in buttons
            ],
        })

    return {
        "name":       spec["name"],
        "language":   spec["lang"],
        "category":   spec.get("category", "UTILITY"),
        "components": components,
    }
    
async def _ensure_seed_templates(db: Client, phone_id: str, host: dict) -> None:
    """Check the DB; anything missing is created through the agent proxy."""
    from routers.template_manager import (
        SEED_TEMPLATES, find_template, supports_templates,
    )

    if not supports_templates(db, phone_id):
        return

    for spec in SEED_TEMPLATES:
        if find_template(db, phone_id, spec["name"], spec["lang"]):
            continue
        try:
            await _agent_post(
                host["ip_address"],
                f"/api/phones/{phone_id}/templates",
                _agent_template_payload(spec),
                timeout=30,
            )
            logger.info(f"[TPL] seeded {spec['name']} phone={phone_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 409:
                logger.warning(f"[TPL] seed {spec['name']} failed: {e.response.text}")
        except httpx.RequestError as e:
            logger.warning(f"[TPL] seed {spec['name']} unreachable: {e}")
            
@router.post("/provision", summary="Provision phone", description="Creates a phone for the current user, or reuses the one with the same number, on a healthy agent host. Seeds the default templates and returns the QR code or pairing code used to link WhatsApp.")
async def provision_phone(
    body: ProvisionRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    clean_number = "".join(filter(str.isdigit, body.phone_number))
    if not clean_number or len(clean_number) < 7:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    # ── בדוק אם קיים לאותו user ──────────────────────────────────────
    existing_res = (
        db.table("phones")
        .select("id, user_id, status, host_id, number")
        .or_(f"number.eq.{clean_number},number.eq.+{clean_number}")
        .eq("user_id", user["uid"])
        .limit(1)
        .execute()
    )
    phone  = existing_res.data[0] if existing_res.data else None
    is_new = phone is None

    logger.info(
        f"[PROVISION] user={user['uid']} number={clean_number} "
        f"existing={'yes' if phone else 'no'}"
    )

    # ── מצא host ──────────────────────────────────────────────────────
    host = None
    if phone:
        host = await _get_host_for_phone(db, phone["id"])
    if not host:
        host = await _find_healthy_host(db, retries=3)
    if not host:
        raise HTTPException(status_code=503, detail="No agent available — all hosts unreachable")

    try:
        data = await _agent_post_with_retry(
            host["ip_address"],
            "/api/phones/provision",
            {
                "phoneNumber": clean_number,
                "nickname":    body.nickname,
                "tag":         body.tag,
                "userId":      user["uid"],
                "isNew":       is_new,
                "usePairingCode": body.use_pairing_code,   # ← 
            },
            retries=3,
            delay=2.0,
            timeout=45,
        )

        phone_id = data.get("phoneId") or (phone["id"] if phone else None)

        if phone_id:
            try:
                await _ensure_seed_templates(db, phone_id, host)
            except Exception as e:
                logger.warning(f"[PROVISION] template seed failed {phone_id}: {e}")
                

        logger.info(
            f"[PROVISION] {'Created' if is_new else 'Reused'} phone "
            f"{clean_number} → id={phone_id}"
        )

        return {
            "phone_id":        phone_id,
            "phone_number":    clean_number,
            "is_new":          is_new,
            "status":          data.get("status", "qr_ready"),
            "qr_image_base64": data.get("qrImageBase64"),
            "qr_code":         data.get("qrCode"),
            "qr_refresh_url":  data.get("qrRefreshUrl"),
            "message":         data.get("message"),
            "host_name":       host["host_name"],
            "pairing_code":    data.get("pairingCode"),     

        }

    except httpx.HTTPStatusError as e:
        logger.error(f"[PROVISION] Agent HTTP error: {e.response.status_code} {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"[PROVISION] Agent unreachable after retries: {e}")
        raise HTTPException(status_code=503, detail="Agent unreachable after 3 attempts")


@router.get("/{phone_id}/qrcode", summary="Get QR code", description="Returns the current QR code, pairing code and connection status of the phone from its agent host.")
async def get_qr_code(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        host = await _find_healthy_host(db)
    if not host:
        raise HTTPException(status_code=503, detail="No agent available")
    try:
        data = await _agent_get(host["ip_address"], f"/api/phones/{phone_id}/qrcode")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Phone not found on agent")
        raise HTTPException(status_code=502, detail="Agent error")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")
    return {
        "status":          data.get("status"),
        "qr_image_base64": data.get("qrImageBase64"),
        "qr_code":         data.get("qr"),
        "message":         data.get("message"),
        "pairing_code":    data.get("pairingCode"),     # ← הוסף

    }

@router.post("/{phone_id}/pairing-code/refresh", summary="Refresh pairing code", description="Asks the agent host to generate a new pairing code for the phone.")
async def refresh_pairing_code(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        host = await _find_healthy_host(db)
    if not host:
        raise HTTPException(status_code=503, detail="No agent available")
    try:
        data = await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/pairing-code/refresh", {}, timeout=45)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")
    return {
        "status":       data.get("status"),
        "pairing_code": data.get("pairingCode"),
        "message":      data.get("message"),
        "poll_url":     data.get("pollUrl"),
    }
    
@router.post("/{phone_id}/pause", summary="Pause phone", description="Pauses the phone on its agent host.")
async def pause_phone(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")
    try:
        return await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/pause", {})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")


@router.post("/{phone_id}/resume", summary="Resume phone", description="Resumes a paused phone on its agent host.")
async def resume_phone(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")
    try:
        return await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/resume", {})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")


@router.post("/{phone_id}/logout", summary="Log out phone", description="Logs the phone out of WhatsApp on its agent host.")
async def logout_phone(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")
    try:
        return await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/logout", {})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")


@router.post("/{phone_id}/send/text", summary="Send text message", description="Sends a WhatsApp text message from the phone through its agent host. Requires jid (or the legacy to) and text.")
async def send_text_message(
    phone_id: str,
    body: SendTextRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")

    jid  = body.target
    text = body.text
    if not jid or not text:
        raise HTTPException(status_code=400, detail="jid (or to) and text are required")

    try:
        return await _agent_post(
            host["ip_address"],
            f"/api/phones/{phone_id}/send/text",
            {"jid": jid, "text": text},
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")


class UpdatePhoneRequest(BaseModel):
    label: Optional[str] = Field(None, description="Display label of the phone.")
    color: Optional[str] = Field(None, description="Display color of the phone.")
    lang: Optional[str] = Field(None, description="Language code of the phone.")


@router.patch("/{phone_id}", summary="Update phone", description="Updates label, color or lang on a phone owned by the current user. No other field can be changed.")
async def update_phone(
    phone_id: str,
    body: UpdatePhoneRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    # Only the fields on the model can be written: a raw dict would let a
    # caller set user_id or creds_base64 directly.
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = (
        db.table("phones")
        .update(patch)
        .eq("id", phone_id)
        .eq("user_id", user["uid"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Phone not found")
    return result.data[0]


@router.delete("/{phone_id}", summary="Delete phone", description="Deletes a phone owned by the current user.")
async def delete_phone(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    result = (
        db.table("phones")
        .delete()
        .eq("id", phone_id)
        .eq("user_id", user["uid"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Phone not found")
    return {"ok": True}


@internal_route(router.patch("/{phone_id}/docker-status", summary="Update docker status", description="Internal. Stores docker_status and docker_url reported for the phone. Body: { status, url }."))
async def update_docker_status(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update({
        "docker_status": body["status"],
        "docker_url":    body.get("url"),
    }).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}


async def _dry_run_template(host: dict, phone_id: str, spec: dict) -> dict:
    """Send one template spec to the agent in validate-only mode."""
    started = datetime.now(timezone.utc)
    result = {"name": spec["name"], "lang": spec["lang"], "ok": False}

    try:
        data = await _agent_post(
            host["ip_address"],
            f"/api/phones/{phone_id}/templates/validate",
            _agent_template_payload(spec),
            timeout=20,
        )
        result["ok"] = True
        result["agent"] = data
    except httpx.HTTPStatusError as e:
        result["status_code"] = e.response.status_code
        result["error"] = e.response.text
    except httpx.RequestError as e:
        result["error"] = f"{type(e).__name__}: {e}"

    result["ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return result


@internal_route(router.post(
    "/{phone_id}/templates/test",
    summary="Dry-run the seed templates",
    description="Internal. Validates every seed template against the agent without writing anything. Run this before provision.",
))
async def test_seed_templates(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    from routers.template_manager import (
        SEED_TEMPLATES, find_template, supports_templates,
        _norm_content, _norm_examples, _validate,
    )

    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")

    checks = []
    for spec in SEED_TEMPLATES:
        # Local validation first — a bad spec never reaches the agent.
        content  = _norm_content(spec["content"])
        examples = _norm_examples(spec["examples"])
        issues   = _validate(spec["name"], spec["lang"], content, examples)

        entry = {
            "name":            spec["name"],
            "lang":            spec["lang"],
            "local_valid":     not issues,
            "local_issues":    issues or [],
            "already_exists":  bool(find_template(db, phone_id, spec["name"], spec["lang"])),
        }

        if issues:
            entry["agent"] = {"skipped": "local validation failed"}
        else:
            entry["agent"] = await _dry_run_template(host, phone_id, spec)

        checks.append(entry)

    return {
        "phone_id":           phone_id,
        "host":               host["host_name"],
        "supports_templates": supports_templates(db, phone_id),
        "total":              len(checks),
        "passed":             sum(1 for c in checks if c["local_valid"] and c["agent"].get("ok")),
        "checks":             checks,
    }


# ── Template seeding & diagnostics ────────────────────────────────────────
# These paths are more specific than /{phone_id}. FastAPI matches in
# declaration order, so never declare a POST /{phone_id} above this block.

async def _create_one_seed(db: Client, phone_id: str, host: dict, name: str) -> dict:
    """Create a single seed template through the agent, if it is missing."""
    from routers.template_manager import SEED_TEMPLATES, find_template

    spec = next((s for s in SEED_TEMPLATES if s["name"] == name), None)
    if not spec:
        raise HTTPException(status_code=404, detail=f"No seed template named {name}")

    existing = find_template(db, phone_id, spec["name"], spec["lang"])
    if existing:
        return {"created": False, "reason": "already exists", "template": existing}

    try:
        data = await _agent_post(
            host["ip_address"],
            f"/api/phones/{phone_id}/templates",
            _agent_template_payload(spec),
            timeout=30,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            return {"created": False, "reason": "agent reports duplicate"}
        logger.error(f"[TPL] create {name} failed: {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")

    logger.info(f"[TPL] created {name} phone={phone_id}")
    return {"created": True, "agent": data}


@internal_route(router.post(
    "/{phone_id}/templates/hello-world",
    summary="Create hello_world",
    description="Internal. Creates the hello_world template through the agent if it does not exist yet.",
))
async def create_hello_world(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")
    return await _create_one_seed(db, phone_id, host, "hello_world")


@internal_route(router.post(
    "/{phone_id}/templates/check-contact",
    summary="Create check_contact",
    description="Internal. Creates the check_contact template through the agent if it does not exist yet.",
))
async def create_check_contact(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")
    return await _create_one_seed(db, phone_id, host, "check_contact")
