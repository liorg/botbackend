# cleanup_job.py
import asyncio, logging, os
from datetime import datetime, timezone
from supabase import create_client

logger = logging.getLogger(__name__)

def _db():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

async def _run_cleanup():
    db  = _db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        expired = (
            db.table("calls")
            .select("id, phone_id, contact_id")
            .eq("status", "active")
            .lt("expected_end", now)
            .execute()
        )
        for call in (expired.data or []):
            db.table("calls").update({
                "status":                 "completed",
                "ended_at":               now,
                "last_status_updated_at": now,
            }).eq("id", call["id"]).execute()

            db.table("webhook_registrations") \
              .delete() \
              .eq("phone_id",   call["phone_id"]) \
              .eq("contact_id", call["contact_id"]) \
              .execute()

            logger.info("[CLEANUP] call=%s closed", call["id"])

    except Exception as e:
        logger.error("[CLEANUP] Error: %s", e)

async def cleanup_loop():
    while True:
        await asyncio.sleep(60)
        await _run_cleanup()

_task = None

def start_cleanup():
    global _task
    _task = asyncio.create_task(cleanup_loop())
    logger.info("[CLEANUP] Started")

def stop_cleanup():
    global _task
    if _task:
        _task.cancel()