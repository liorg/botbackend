# routers/proxy_media.py
import io
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/messages", tags=["media"])  # ← חסר!

BACKEND_URL = os.getenv("BACKEND_URL", "https://vid.michal-solutions.com/api")

async def _get_agent_api_port(db: Client, phone_id: str):
    try:
        res = (
            db.table("phones")
            .select("api_port, agent_hosts(ip_address)")
            .eq("id", phone_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None, None
        phone    = res.data[0]
        api_port = phone.get("api_port")
        host     = phone.get("agent_hosts") or {}
        ip       = host.get("ip_address")
        return ip, api_port
    except Exception:
        return None, None

@router.get("/media/{phone_id}/{message_id}")
async def proxy_media(
    phone_id: str,
    message_id: str,
    db: Client = Depends(get_supabase),
):
    ip, api_port = await _get_agent_api_port(db, phone_id)
    if not ip or not api_port:
        raise HTTPException(404, "Agent not found")

    agent_url = f"http://{ip}:{api_port}/media/{message_id}"

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(agent_url)
            if r.status_code == 404:
                raise HTTPException(404, "Media not found")
            return StreamingResponse(
                io.BytesIO(r.content),
                media_type=r.headers.get("content-type", "application/octet-stream")
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Agent unavailable: {e}")