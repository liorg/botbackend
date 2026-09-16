# routers/notifications.py
from fastapi import APIRouter, Depends, HTTPException, Query
from dependencies import get_supabase
from supabase import Client
from pydantic import BaseModel, Field
from typing import Optional, Literal
import uuid

router = APIRouter(prefix="/notifications", tags=["notifications"])

LogLevel = Literal["info", "success", "warning", "error"]


# ── Schemas ────────────────────────────────────────────────────────────────

class NotificationCreate(BaseModel):
    user_id: str = Field(..., description="User who receives the notification.")
    phone_id: Optional[str] = Field(None, description="Phone the notification relates to, if any.")
    title: str = Field(..., description="Short headline shown in the notifications list.")
    message: str = Field(..., description="Notification body text.")
    log_level: LogLevel = Field("info", description="Severity: info, success, warning or error.")
    is_send: bool = Field(False, description="Stored in is_send; marks the notification as sent.")
    source: Optional[str] = Field(None, description="Where the notification came from, for example the raising service.")
    extra: Optional[dict] = Field(None, description="Free-form JSON with extra context.")


class MarkReadBody(BaseModel):
    ids: list[str] = Field(default_factory=list, description="Notification ids to mark as read. Leave empty to mark all unread notifications.")  # empty = mark ALL


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/", summary="List notifications", description="Returns notifications newest first, paged with limit/offset. Set unread_only=true to get only unread ones.")
async def list_notifications(
    limit: int = Query(50, description="Maximum number of notifications to return."),
    offset: int = Query(0, description="Number of notifications to skip, for paging."),
    unread_only: bool = Query(False, description="Return only unread notifications."),
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


@router.get("/unread-count", summary="Count unread notifications", description="Returns the number of unread notifications as { count }.")
async def unread_count(db: Client = Depends(get_supabase)):
    result = (
        db.table("notifications")
        .select("id", count="exact")
        .eq("is_read", False)
        .execute()
    )
    return {"count": result.count or 0}


@router.post("/mark-read", summary="Mark notifications as read", description="Marks the given ids as read. An empty ids list marks every unread notification as read.")
async def mark_read(body: MarkReadBody, db: Client = Depends(get_supabase)):
    if body.ids:
        db.table("notifications").update({"is_read": True}).in_("id", body.ids).execute()
    else:
        db.table("notifications").update({"is_read": True}).eq("is_read", False).execute()
    return {"ok": True}


@router.post("/", summary="Create notification", description="Inserts a new unread notification and returns the created row.")
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


@router.delete("/{notification_id}", summary="Delete notification", description="Deletes a notification by id. Returns { ok: true } even when the id does not exist.")
async def delete_notification(notification_id: str, db: Client = Depends(get_supabase)):
    db.table("notifications").delete().eq("id", notification_id).execute()
    return {"ok": True}