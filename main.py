# main.py
import asyncio
import contextlib
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import Client, create_client

from routers import (
    auth,  phones,  contacts,  scenarios,   schedules,   calls,messages,  proxy_media,  phones_contacts,  webhook_registrations, notifications, active_chats,contact_calls, wa_override_ab,phone_admin
)
from config import APP_MODE
from routers.template_manager import router as templates_router
from routers.compile_check import compile_router
from logging_config import get_logger, logging_middleware

load_dotenv()

version = "2.0.0.2"
logger = get_logger("main")

BACKEND_URL = os.getenv("BACKEND_URL", "https://vid.michal-solutions.com/api").rstrip("/")
RECORDING_WEBHOOK_URL = os.getenv(
    "RECORDING_WEBHOOK_URL",
    os.getenv(
        "LISTENER_WEBHOOK_URL",
        f"{BACKEND_URL}/webhook-registrations/callback",
    ),
)
WEBHOOK_TYPE_RECORDING = "recording"
CALL_EXPIRY_INTERVAL_SECONDS = max(
    int(os.getenv("CALL_EXPIRY_INTERVAL_SECONDS", "30")),
    10,
)

app = FastAPI(title="ScenarioBot API", version=version)

if APP_MODE not in {"client", "internal"}:
    raise RuntimeError(
        f"Invalid APP_MODE: {APP_MODE}. Expected 'client' or 'internal'."
    )
# ── Infrastructure helpers ────────────────────────────────────
def _create_service_db() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
    )

    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required"
        )

    return create_client(url, key)


def _ensure_recording_webhook(db: Client) -> None:
    """Ensure the permanent recording webhook registration exists and remains active."""
    now = datetime.now(timezone.utc).isoformat()

    db.table("webhook_registrations").upsert(
        {
            "callback_url": RECORDING_WEBHOOK_URL,
            "type": WEBHOOK_TYPE_RECORDING,
            "status": "active",
            "is_active": True,
            "created_at": now,
        },
        on_conflict="callback_url,type",
    ).execute()

def _expire_recording_calls(db: Client) -> int:
    """Mark only expired, still-active recording playback calls as expired."""
    now = datetime.now(timezone.utc).isoformat()

    result = (
        db.table("calls")
        .update(
            {
                "status": "expired",
                "ended_at": now,
                "last_status_updated_at": now,
            }
        )
        .eq("call_type", "recording")
        .eq("status", "active")
        .lt("expected_end", now)
        .execute()
    )

    return len(result.data or [])


async def _recording_expiry_worker() -> None:
    while True:
        try:
            expired_count = await asyncio.to_thread(
                _expire_recording_calls,
                app.state.service_db,
            )
            if expired_count:
                logger.info(
                    "Expired recording calls",
                    extra={"count": expired_count},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recording expiry worker failed")

        await asyncio.sleep(CALL_EXPIRY_INTERVAL_SECONDS)


# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Origin", "X-Requested-With"],
    expose_headers=["Content-Length", "X-Request-Id"],
    max_age=600,
)

app.middleware("http")(logging_middleware)
if APP_MODE == "internal":
    # ── Routers ───────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/api")
    app.include_router(phone_admin.router, prefix="/api")   # ← לפני phones
    app.include_router(phones.router, prefix="/api")
    app.include_router(contacts.router, prefix="/api")
    app.include_router(scenarios.router, prefix="/api")
    app.include_router(schedules.router, prefix="/api")
    app.include_router(calls.router, prefix="/api")
    app.include_router(messages.router, prefix="/api")
    app.include_router(proxy_media.router, prefix="/api")
    app.include_router(phones_contacts.router, prefix="/api")
    app.include_router(webhook_registrations.router, prefix="/api")
    app.include_router(compile_router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(active_chats.router, prefix="/api")
    app.include_router(contact_calls.router, prefix="/api")  
    app.include_router(templates_router, prefix="/api")
    app.include_router(wa_override_ab.router, prefix="/api")   
    
elif APP_MODE == "client":
    # בהתחלה רק endpoints שאתה באמת רוצה לחשוף
    app.include_router(phones.router, prefix="/api")
    
# ── Startup / Shutdown ────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info(
        f"ScenarioBot API starting - APP_MODE={APP_MODE}",
        extra={
            "action": "startup",
            "version": version,
            "environment": os.getenv("ENV", "production"),
            "app_mode": APP_MODE,
        },
    )

    # רק INTERNAL מקבל service_role ועושה webhook registration
    if APP_MODE == "internal":
        app.state.service_db = _create_service_db()

        await asyncio.to_thread(
            _ensure_recording_webhook,
            app.state.service_db,
        )

        logger.info(
            "Internal mode - service DB initialized and recording webhook ensured"
        )

    else:
        app.state.service_db = None

        logger.info(
            "Client mode - service DB and recording webhook disabled"
        )

@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "recording_expiry_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    logger.info("ScenarioBot API shutting down", extra={"action": "shutdown"})


# ── Endpoints ────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"name": "ScenarioBot", "version": version, "status": "running"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": version,
        "app_mode": APP_MODE,
    }


@app.get("/whoami")
def whoami(
    authorization: str | None = Header(None)
):
    # INTERNAL
    if APP_MODE == "internal":
        db = _create_service_db()

        result = (
            db.table("users")
            .select("count", count="exact")
            .execute()
        )

        return {
            "status": "ok",
            "version": version,
            "app_mode": APP_MODE,
            "supabase": "connected",
            "users_count": result.count,
        }

    # CLIENT
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header",
        )

    token = authorization[7:].strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing token",
        )

    db = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_ANON_KEY"],
    )

    try:
        response = db.auth.get_user(token)

        if not response.user:
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
            )

        return {
            "status": "ok",
            "version": version,
            "app_mode": APP_MODE,
            "id": response.user.id,
            "email": response.user.email,
        }

    except HTTPException:
        raise

    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )
