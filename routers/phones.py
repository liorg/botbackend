"""
phones.py — FastAPI router
Proxy between UI and .NET Agent (WhatsAppDockerManager)

Flow:
  1. UI calls POST /api/phones/provision
  2. FastAPI finds an available agent host from Supabase (agent_hosts table)
  3. FastAPI checks GET http://{host_ip}:{agent_port}/api/host/health
  4. FastAPI forwards provision request to the healthy agent
  5. FastAPI returns QR / connected status to UI

Polling:
  UI calls GET /api/phones/{phone_id}/qrcode every N seconds
  FastAPI forwards to the agent that owns that phone (via host_id → ip_address)
"""

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
AGENT_PORT        = int(os.getenv("AGENT_PORT", "5000"))       # .NET agent port
AGENT_TOKEN       = os.getenv("AGENT_TOKEN", "")               # shared secret
AGENT_TIMEOUT     = float(os.getenv("AGENT_TIMEOUT", "10"))    # seconds
HOST_HEARTBEAT_TIMEOUT_MINUTES = int(os.getenv("HOST_HEARTBEAT_TIMEOUT", "5"))

# ── Request / Response models ─────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    phone_number: str
    nickname:     Optional[str] = None
    tag:          Optional[str] = None

class QrPollResponse(BaseModel):
    status:          str            # "qr_ready" | "connected" | "pending"
    qr_image_base64: Optional[str] = None
    qr_code:         Optional[str] = None
    message:         Optional[str] = None


# ── Agent HTTP helper ─────────────────────────────────────────────────────────

def _agent_headers() -> dict:
    """Auth header sent to .NET agent"""
    return {
        "X-Agent-Token": AGENT_TOKEN,
        "Content-Type":  "application/json",
    }


async def _agent_get(ip: str, path: str) -> dict:
    """GET request to agent"""
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
        resp = await client.get(url, headers=_agent_headers())
        resp.raise_for_status()
        return resp.json()


async def _agent_post(ip: str, path: str, body: dict) -> dict:
    """POST request to agent"""
    url = f"http://{ip}:{AGENT_PORT}{path}"
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
        resp = await client.post(url, headers=_agent_headers(), json=body)
        resp.raise_for_status()
        return resp.json()


# ── Host selection helpers ────────────────────────────────────────────────────

async def _get_active_hosts(db: Client) -> list[dict]:
    """
    Fetch agent_hosts from Supabase where:
    - status = 'active'
    - last_heartbeat within HOST_HEARTBEAT_TIMEOUT_MINUTES
    """
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=HOST_HEARTBEAT_TIMEOUT_MINUTES)
    ).isoformat()

    result = (
        db.table("agent_hosts")
        .select("id, host_name, ip_address, external_ip, max_containers, port_range_start, port_range_end, last_heartbeat")
        .eq("status", "active")
        .gt("last_heartbeat", cutoff)
        .execute()
    )
    return result.data or []


async def _check_host_health(ip: str) -> bool:
    """
    Call GET /api/host/health on the agent.
    Returns True if agent responds with status 200.
    """
    try:
        data = await _agent_get(ip, "/api/host/health")
        return data.get("status") == "healthy"
    except Exception as e:
        logger.warning(f"Health check failed for {ip}: {e}")
        return False


async def _find_healthy_host(db: Client) -> Optional[dict]:
    """
    Find first agent host that:
    1. Is active in Supabase (heartbeat fresh)
    2. Responds to /api/host/health
    """
    hosts = await _get_active_hosts(db)

    if not hosts:
        logger.error("No active hosts found in Supabase agent_hosts table")
        return None

    for host in hosts:
        ip = host.get("ip_address")
        if not ip:
            continue
        healthy = await _check_host_health(ip)
        if healthy:
            logger.info(f"Healthy host found: {host['host_name']} ({ip})")
            return host
        else:
            logger.warning(f"Host {host['host_name']} ({ip}) failed health check")

    return None


async def _get_host_for_phone(db: Client, phone_id: str) -> Optional[dict]:
    """Get the agent host assigned to a specific phone via host_id"""
    phone_result = (
        db.table("phones")
        .select("host_id")
        .eq("id", phone_id)
        .execute()
    )
    if not phone_result.data:
        return None

    host_id = phone_result.data[0].get("host_id")
    if not host_id:
        return None

    host_result = (
        db.table("agent_hosts")
        .select("id, host_name, ip_address")
        .eq("id", host_id)
        .execute()
    )
    return host_result.data[0] if host_result.data else None


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/")
async def list_phones(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase)
):
    """List all phones for the current user"""
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
    db: Client = Depends(get_supabase)
):
    """
    Step 1 + 2: Provision a phone via the .NET agent.
    - Finds a healthy agent host from Supabase
    - Forwards provision request to agent
    - Returns phoneId + QR code (base64) or 'connected' status
    """
    logger.info(f"Provision request: {body.phone_number} by user {user['uid']}")

    # ── 1. Find healthy agent host ─────────────────────────────────────────
    host = await _find_healthy_host(db)
    if not host:
        raise HTTPException(
            status_code=503,
            detail="No available agent host. All hosts are offline or unreachable."
        )

    host_ip = host["ip_address"]

    # ── 2. Forward provision to agent ─────────────────────────────────────
    try:
        agent_response = await _agent_post(host_ip, "/api/phones/provision", {
            "phoneNumber": body.phone_number,
            "nickname":    body.nickname,
            "tag":         body.tag,
            "userId":      user["uid"],
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Agent provision failed: {e.response.status_code} — {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"Agent unreachable during provision: {e}")
        raise HTTPException(status_code=503, detail="Agent unreachable during provision")

    logger.info(f"Agent provision response: status={agent_response.get('status')} phoneId={agent_response.get('phoneId')}")

    return {
        "phone_id":        agent_response.get("phoneId"),
        "phone_number":    agent_response.get("phoneNumber"),
        "label":           agent_response.get("label"),
        "status":          agent_response.get("status"),          # "connected" | "qr_ready"
        "qr_image_base64": agent_response.get("qrImageBase64"),
        "qr_code":         agent_response.get("qrCode"),
        "qr_refresh_url":  agent_response.get("qrRefreshUrl"),
        "message":         agent_response.get("message"),
        "host_name":       host["host_name"],
    }


@router.get("/{phone_id}/qrcode")
async def get_qr_code(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase)
):
    """
    Step 2 polling: Get QR code or connection status for a phone.
    UI polls this every 3-5 seconds until status = 'connected'.
    """
    # ── Find which host owns this phone ───────────────────────────────────
    host = await _get_host_for_phone(db, phone_id)

    if not host:
        # Fallback: find any healthy host (phone may not have host_id yet)
        host = await _find_healthy_host(db)
        if not host:
            raise HTTPException(status_code=503, detail="No agent available")

    host_ip = host["ip_address"]

    # ── Forward to agent ──────────────────────────────────────────────────
    try:
        data = await _agent_get(host_ip, f"/api/phones/{phone_id}/qrcode")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Phone not found on agent")
        raise HTTPException(status_code=502, detail="Agent error")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")

    return {
        "status":          data.get("status"),           # "connected" | "qr_ready"
        "qr_image_base64": data.get("qrImageBase64"),
        "qr_code":         data.get("qr"),
        "message":         data.get("message"),
    }


@router.get("/agents/health")
async def check_all_agents(
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase)
):
    """
    Returns health status of all agent hosts.
    Used by UI to show which agents are online.
    """
    hosts = await _get_active_hosts(db)
    results = []

    for host in hosts:
        ip = host.get("ip_address", "")
        healthy = await _check_host_health(ip) if ip else False
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
    db: Client = Depends(get_supabase)
):
    """Logout phone — triggers fresh QR scan"""
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")

    try:
        data = await _agent_post(host["ip_address"], f"/api/phones/{phone_id}/logout", {})
        return data
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Agent unreachable")


@router.patch("/{phone_id}")
async def update_phone(
    phone_id: str,
    body: dict,
    db: Client = Depends(get_supabase)
):
    """Update phone metadata in Supabase"""
    result = db.table("phones").update(body).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{phone_id}")
async def delete_phone(
    phone_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase)
):
    """Delete phone from Supabase"""
    db.table("phones").delete().eq("id", phone_id).execute()
    return {"ok": True}


@router.patch("/{phone_id}/docker-status")
async def update_docker_status(
    phone_id: str,
    body: dict,
    db: Client = Depends(get_supabase)
):
    """Called by agent to update docker status"""
    result = db.table("phones").update({
        "docker_status": body["status"],
        "docker_url":    body.get("url"),
    }).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}