# routers/messages.py או routers/media.py
import io
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from dependencies import get_supabase, get_current_user
from supabase import Client

@router.get("/media/{phone_id}/{message_id}")
async def proxy_media(
    phone_id: str,
    message_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Proxy תמונות/אודיו — מסתיר את ה-agent"""
    # ── שלוף agent IP מה-DB ──────────────────────────────────────
    agent_info = await _get_agent_ip_for_phone(db, phone_id)
    if not agent_info:
        raise HTTPException(404, "Agent not found")
    
    agent_ip, _ = agent_info
    
    # ── השתמש ב-FastAPI port (8970 וכו') ─────────────────────────
    phone_res = db.table("phones").select("api_port").eq("id", phone_id).limit(1).execute()
    api_port  = (phone_res.data or [{}])[0].get("api_port", 8000)
    
    agent_url = f"http://{agent_ip}:{api_port}/media/{message_id}"
    
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