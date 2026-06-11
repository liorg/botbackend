# main.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv

from routers import (
    auth, phones, contacts, scenarios, schedules,
    calls, messages, proxy_media, phones_contacts,
    webhook_registrations,
)
from routers.compile_check import compile_router
from logging_config import get_logger, logging_middleware

load_dotenv()

version = "1.0.3.0"
logger = get_logger("main")

app = FastAPI(title="ScenarioBot API", version=version)

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

# ── Routers ───────────────────────────────────────────────────
app.include_router(auth.router,                   prefix="/api")
app.include_router(phones.router,                 prefix="/api")
app.include_router(contacts.router,               prefix="/api")
app.include_router(scenarios.router,              prefix="/api")
app.include_router(schedules.router,              prefix="/api")
app.include_router(calls.router,                  prefix="/api")
app.include_router(messages.router,               prefix="/api")
app.include_router(proxy_media.router,            prefix="/api")
app.include_router(phones_contacts.router,        prefix="/api")
app.include_router(webhook_registrations.router,  prefix="/api")
app.include_router(compile_router,                prefix="/api")  # ✅ compile_router

# ── Startup / Shutdown ────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("ScenarioBot API starting", extra={
        "action": "startup",
        "version": version,
        "environment": os.getenv("ENV", "production"),
    })

@app.on_event("shutdown")
async def shutdown():
    logger.info("ScenarioBot API shutting down", extra={"action": "shutdown"})

# ── Endpoints ────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"name": "ScenarioBot", "version": version, "status": "running"}

@app.get("/health")
def health():
    return {"status": "ok", "version": version}

@app.get("/whoami")
def whoami():
    db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    result = db.table("users").select("count", count="exact").execute()
    return {"status": "ok", "supabase": "connected", "users_count": result.count}
