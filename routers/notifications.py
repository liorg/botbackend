# routers/notifications.py
from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_supabase
from supabase import Client
from pydantic import BaseModel, Field
from typing import Optional, Literal
import uuid

router = APIRouter(prefix="/notifications", tags=["notifications"])

LogLevel = Literal["info", "success", "warning", "error"]


# ── Schemas ────────────────────────────────────────────────────────────────

class NotificationCreate(BaseModel):
    user_id: str
    phone_id: Optional[str] = None
    title: str
    message: str
    log_level: LogLevel = "info"
    is_send: bool = False
    source: Optional[str] = None
    extra: Optional[dict] = None


class MarkReadBody(BaseModel):
    ids: list[str] = Field(default_factory=list)  # empty = mark ALL


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/")
async def list_notifications(
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    db: Client = Depends(get_supabase),
):
    q = (
        db.table("notifications")
        .select("id, user_id, phone_id, title, message, log_level, is_read, is_send, source, extra, created_at")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if unread_only:
        q = q.eq("is_read", False)

    result = q.execute()
    return result.data or []


@router.get("/unread-count")
async def unread_count(db: Client = Depends(get_supabase)):
    result = (
        db.table("notifications")
        .select("id", count="exact")
        .eq("is_read", False)
        .execute()
    )
    return {"count": result.count or 0}


@router.post("/mark-read")
async def mark_read(body: MarkReadBody, db: Client = Depends(get_supabase)):
    if body.ids:
        db.table("notifications").update({"is_read": True}).in_("id", body.ids).execute()
    else:
        db.table("notifications").update({"is_read": True}).eq("is_read", False).execute()
    return {"ok": True}


@router.post("/")
async def create_notification(body: NotificationCreate, db: Client = Depends(get_supabase)):
    payload = {
        "id":        str(uuid.uuid4()),
        "user_id":   body.user_id,
        "title":     body.title,
        "message":   body.message,
        "log_level": body.log_level,
        "is_send":   body.is_send,
        "is_read":   False,
    }
    if body.phone_id: payload["phone_id"] = body.phone_id
    if body.source:   payload["source"]   = body.source
    if body.extra:    payload["extra"]    = body.extra

    result = db.table("notifications").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create notification")
    return result.data[0]


@router.delete("/{notification_id}")
async def delete_notification(notification_id: str, db: Client = Depends(get_supabase)):
    db.table("notifications").delete().eq("id", notification_id).execute()
    return {"ok": True}