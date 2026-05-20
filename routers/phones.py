"""
phones.py — FastAPI router
Proxy between UI and .NET Agent (WhatsAppDockerManager)
"""

import os
import asyncio
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

AGENT_PORT    = int(os.getenv("AGENT_PORT", "5000"))
AGENT_TOKEN   = os.getenv("AGENT_TOKEN", "")
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "10"))
HOST_HEARTBEAT_TIMEOUT_MINUTES = int(os.getenv("HOST_HEARTBEAT_TIMEOUT", "60"))
BLOCKED_IPS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


class ProvisionRequest(BaseModel):
    phone_number: str
    nickname:     Optional[str] = None
    tag:          Optional[str] = None


def _agent_headers() -> dict:
    return {"X-Agent-Token": AGENT_TOKEN, "Content-Type": "application/json"}


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


async def _agent_post_with_retry(
    ip: str, path: str, body: dict,
    retries: int = 3, delay: float = 2.0
) -> dict:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return await _agent_post(ip, path, body)
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            last_error = e
            logger.warning(
                f"[AGENT] Attempt {attempt}/{retries} failed for {ip}{path}: {e}"
            )
            if attempt < retries:
                await asyncio.sleep(delay * attempt)  # 2s, 4s, 6s
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

@router.get("/")
async def list_phones(user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    result = db.table("phones").select("*").eq("user_id", user["uid"]).execute()
    return result.data


@router.get("/agents/health")
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


@router.post("/provision")
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
            },
            retries=3,
            delay=2.0,
        )

        phone_id = data.get("phoneId") or (phone["id"] if phone else None)

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
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"[PROVISION] Agent HTTP error: {e.response.status_code} {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Agent error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"[PROVISION] Agent unreachable after retries: {e}")
        raise HTTPException(status_code=503, detail="Agent unreachable after 3 attempts")


@router.get("/{phone_id}/qrcode")
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
    }


@router.post("/{phone_id}/pause")
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


@router.post("/{phone_id}/resume")
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


@router.post("/{phone_id}/logout")
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


@router.post("/{phone_id}/send/text")
async def send_text_message(
    phone_id: str,
    body: dict,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    host = await _get_host_for_phone(db, phone_id)
    if not host:
        raise HTTPException(status_code=404, detail="Phone host not found")

    jid  = body.get("jid")
    text = body.get("text")
    if not jid or not text:
        raise HTTPException(status_code=400, detail="jid and text are required")

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


@router.patch("/{phone_id}")
async def update_phone(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update(body).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{phone_id}")
async def delete_phone(phone_id: str, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    db.table("phones").delete().eq("id", phone_id).execute()
    return {"ok": True}


@router.patch("/{phone_id}/docker-status")
async def update_docker_status(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update({
        "docker_status": body["status"],
        "docker_url":    body.get("url"),
    }).eq("id", phone_id).execute()
    return result.data[0] if result.data else {}
