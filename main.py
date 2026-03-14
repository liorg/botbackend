#main.py
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import auth, phones, contacts, scenarios, schedules, calls
from supabase import create_client
from dotenv import load_dotenv

# Import centralized logging
from logging_config import get_logger, logging_middleware
version="1.0.6"
load_dotenv()  # ← חייב להיות לפני הכל

logger = get_logger("main")

app = FastAPI(title="ScenarioBot API", version=version)

# Allowed origins - add your domains here
ALLOWED_ORIGINS = [
    # Production - Vercel
    "https://ui.michal-solutions.com",
    "https://www.ui.michal-solutions.com",
    # API domain (if needed)
    "https://vid.michal-solutions.com",
    # Development
    "http://localhost:5173",  # Vite dev server
    "http://localhost:3000",  # Alternative dev server
    "http://127.0.0.1:5173",
]

# In development, you might want to allow all origins
if os.getenv("ENV", "production") == "development":
    ALLOWED_ORIGINS = ["*"]
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "Accept",
        "Origin",
        "X-Requested-With",
    ],
    expose_headers=["Content-Length", "X-Request-Id"],
    max_age=600,  # Cache preflight requests for 10 minutes
)

# ── Logging Middleware ────────────────────────────────────────
app.middleware("http")(logging_middleware)

# ── Routers ───────────────────────────────────────────────────
app.include_router(auth.router, prefix="/api")
app.include_router(phones.router, prefix="/api")
app.include_router(contacts.router, prefix="/api")
app.include_router(scenarios.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")
app.include_router(calls.router, prefix="/api")

# ── Startup/Shutdown Events ───────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("ScenarioBot API starting", extra={
        "action": "startup",
        "version": version,
        "environment": os.getenv("ENV", "production")
    })

@app.on_event("shutdown")
async def shutdown():
    logger.info("ScenarioBot API shutting down", extra={"action": "shutdown"})

# ── Root Endpoint ────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "name": "ScenarioBot API",
        "version":version,
        "status": "running"
    }

# ── Health ────────────────────────────────────────────────────
@app.get("/health")
def health():
    logger.info("Health check", extra={"action": "health_check"})   
    return {"status": "ok", "version": version}

@app.get("/whoami")
def whoami():
    db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    result = db.table("users").select("count", count="exact").execute()
    return {
        "status": "ok",
        "supabase": "connected",
        "users_count": result.count
    }
