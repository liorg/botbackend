from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import auth, phones, contacts, scenarios, schedules, calls

app = FastAPI(title="ScenarioBot API", version="1.0.0")

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-app.vercel.app",  # ← החלף בדומיין שלך
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(phones.router)
app.include_router(contacts.router)
app.include_router(scenarios.router)
app.include_router(schedules.router)
app.include_router(calls.router)

# ── Health ────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
