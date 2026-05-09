
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, timezone

from dependencies import get_supabase, get_current_user
from supabase import Client
from logging_config import get_logger

logger = get_logger("phones")

router = APIRouter(prefix="/phones", tags=["phones"])

# ── Config ────────────────────────────────────────────────────────────────────
AGENT_PORT                    = int(os.getenv("AGENT_PORT", "5000"))
AGENT_TOKEN                   = os.getenv("AGENT_TOKEN", "")
AGENT_TIMEOUT                 = float(os.getenv("AGENT_TIMEOUT", "10"))
HOST_HEARTBEAT_TIMEOUT_MINUTES = int(os.getenv("HOST_HEARTBEAT_TIMEOUT", "60"))

# IPs that are never valid agents (dev / loopback)
BLOCKED_IPS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


# ── Request models ────────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    phone_number: str
    nickname:     Optional[str] = None
    tag:          Optional[str] = None


# ── Agent HTTP helpers ────────────────────────────────────────────────────────

def _agent_headers() -> dict:
    return {
        "X-Agent-Token": AGENT_TOKEN,
        "Content-Type":  "application/json",
    }


async def _agent_get(ip: str, path: str) -> dict:
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
        resp = await client.get(url, headers=_agent_headers())
        resp.raise_for_status()
        return resp.json()


async def _agent_post(ip: str, path: str, body: dict) -> dict:
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
        resp = await client.post(url, headers=_agent_headers(), json=body)
        resp.raise_for_status()
        return resp.json()


# ── Host selection ────────────────────────────────────────────────────────────

def _is_valid_agent_ip(ip: str) -> bool:
    """
    Block loopback / dev machine IPs.
    Only real routable addresses are valid agents.
    """
    if not ip:
        return False
    ip_stripped = ip.strip().lower()
    if ip_stripped in BLOCKED_IPS:
        logger.warning(f"Skipping blocked IP: {ip}")
        return False
    # Block entire 127.x.x.x range
    if ip_stripped.startswith("127."):
        logger.warning(f"Skipping loopback range IP: {ip}")
        return False
    return True


async def _get_active_hosts(db: Client) -> list[dict]:
    """
    Fetch agent_hosts from Supabase:
    - status = 'active'
    - last_heartbeat within timeout window
    - ip_address is not a loopback / dev address
    """
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=HOST_HEARTBEAT_TIMEOUT_MINUTES)
    ).isoformat()

    result = (
        db.table("agent_hosts")
        .select("id, host_name, ip_address, external_ip, max_containers, last_heartbeat")
        .eq("status", "active")
        .gt("last_heartbeat", cutoff)
        .execute()
    )

    all_hosts = result.data or []

    # Filter out dev / loopback IPs
    valid_hosts = [h for h in all_hosts if _is_valid_agent_ip(h.get("ip_address", ""))]

    if len(all_hosts) != len(valid_hosts):
        skipped = len(all_hosts) - len(valid_hosts)
        logger.info(f"Skipped {skipped} host(s) with loopback/dev IPs")

    return valid_hosts


async def _check_host_health(ip: str, db: Client = None, host_id: str = None) -> bool:
    try:
        data = await _agent_get(ip, "/api/host/health")
        is_healthy = data.get("status") == "healthy"
        
        # עדכן heartbeat אוטומטית כשהשרת מגיב
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


async def _find_healthy_host(db: Client) -> Optional[dict]:
    hosts = await _get_active_hosts(db)

    if not hosts:
        logger.error("No valid (non-loopback) active hosts found in agent_hosts")
        return None

    for host in hosts:
        ip = host.get("ip_address", "")
        if await _check_host_health(ip, db, host["id"]):
            logger.info(f"Healthy host: {host['host_name']} ({ip})")
            return host
        logger.warning(f"Host {host['host_name']} ({ip}) failed health check")

    return None


async def _get_host_for_phone(db: Client, phone_id: str) -> Optional[dict]:
    phone_res = (
        db.table("phones")
        .select("host_id")
        .eq("id", phone_id)
        .execute()
    )
    if not phone_res.data:
        return None

    host_id = phone_res.data[0].get("host_id")
    if not host_id:
        return None

    host_res = (
        db.table("agent_hosts")
        .select("id, host_name, ip_address")
        .eq("id", host_id)
        .execute()
    )
    if not host_res.data:
        return None

    host = host_res.data[0]

    # Safety: never use a loopback IP even if stored in DB
    if not _is_valid_agent_ip(host.get("ip_address", "")):
        logger.error(f"Phone {phone_id} is assigned to a loopback/dev host — refusing")
        return None

    return host


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/")
async def list_phones(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("phones")
        .select("*")
        .eq("user_id", user["uid"])
        .execute()
    )
    return result.data


@router.post("/provision")
async def provision_phone(
    body: ProvisionRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """
    Provision a WhatsApp phone via the .NET agent.
    Returns QR image (base64) or 'connected' if already linked.
    """
    logger.info(f"Provision: {body.phone_number} by user {user['uid']}")

    host = await _find_healthy_host(db)
    if not host:
        raise HTTPException(
            status_code=503,
            detail="No available agent host — all hosts are offline, unreachable, or dev-only IPs",
        )

    try:
        data = await _agent_post(host["ip_address"], "/api/phones/provision", {
            "phoneNumber": body.phone_number,
            "nickname":    body.nickname,
            "tag":         body.tag,
            "userId":      user["uid"],
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Agent provision error {e.response.status_code}: {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"Agent unreachable: {e}")
        raise HTTPException(status_code=503, detail="Agent unreachable during provision")

    return {
        "phone_id":        data.get("phoneId"),
        "phone_number":    data.get("phoneNumber"),
        "label":           data.get("label"),
        "status":          data.get("status"),           # "connected" | "qr_ready"
        "qr_image_base64": data.get("qrImageBase64"),
        "qr_code":         data.get("qrCode"),
        "qr_refresh_url":  data.get("qrRefreshUrl"),
        "message":         data.get("message"),
        "host_name":       host["host_name"],
    }


@router.get("/{phone_id}/qrcode")
async def get_qr_code(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Poll QR status. Returns 'connected' when phone is linked."""
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
    }


@router.get("/agents/health")
async def agents_health(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Health status of all agent hosts (filters dev IPs)."""
    hosts = await _get_active_hosts(db)  # already filtered
    results = []

    for host in hosts:
        ip      = host.get("ip_address", "")
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


@router.post("/{phone_id}/logout")
async def logout_phone(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")
    try:
        return await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/logout", {})
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")


@router.patch("/{phone_id}")
async def update_phone(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update(body).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{phone_id}")
async def delete_phone(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    db.table("phones").delete().eq("id", phone_id).execute()
    return {"ok": True}


@router.patch("/{phone_id}/docker-status")
async def update_docker_status(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update({
        "docker_status": body["status"],
        "docker_url":    body.get("url"),
    }).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}