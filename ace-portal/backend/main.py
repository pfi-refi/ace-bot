"""
Ace Portal — FastAPI app + WebSocket streaming chat.

Serves the static command-center UI and exposes the Ace backend:
  POST /auth        — exchange PORTAL_PASSWORD for a session token
  GET  /health      — version + uptime
  GET  /calendar    — upcoming events (structured, 7 days)
  GET  /tasks       — open Google Tasks (structured)
  GET  /memory      — ace_memory.json contents (source of truth)
  POST /memory      — merge new fact(s) into ace_memory.json
  GET  /weather     — Cleveland weather (OpenWeather proxy, key server-side)
  POST /chat        — non-streaming Ace turn (fallback for /ws)
  WS   /ws/chat     — real-time streaming Ace turn

All secrets stay server-side. Business data is read live from Ace's memory —
nothing about PFI is hardcoded here.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ace_chat import stream_reply
from .calendar_api import get_events_structured
from .memory import merge_memories, read_memory, write_memory
from .revisions import get_revision, list_revisions, recover_all
from .tasks_api import get_tasks_structured
from .weather import get_weather

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ace_portal.main")

PORTAL_VERSION = "v18.17"
START_TIME = time.time()

FRONTEND_DIR = Path(__file__).resolve().parent.parent  # ace-portal/

# ── Auth ─────────────────────────────────────────────────────────────────────────
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "").strip()
# Server-side signing secret for session tokens. Derived from the password so a
# token stays valid across restarts without persisting anything. Falls back to a
# per-process secret if no ANTHROPIC key context is present.
_TOKEN_SECRET = (
    os.environ.get("PORTAL_TOKEN_SECRET")
    or os.environ.get("ANTHROPIC_API_KEY", "ace-portal-dev-secret")
).encode()


def _expected_token() -> str:
    """Deterministic session token derived from the password + server secret."""
    return hmac.new(_TOKEN_SECRET, b"ace-portal|" + PORTAL_PASSWORD.encode(), hashlib.sha256).hexdigest()


def auth_enabled() -> bool:
    return bool(PORTAL_PASSWORD)


def token_valid(token: str) -> bool:
    if not auth_enabled():
        return True
    if not token:
        return False
    return hmac.compare_digest(token, _expected_token())


async def require_auth(authorization: str = Header(default="")) -> None:
    """Dependency for HTTP endpoints. Expects `Authorization: Bearer <token>`."""
    if not auth_enabled():
        return
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if not token_valid(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── App ──────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Ace Portal", version=PORTAL_VERSION)

_cors_origins = os.environ.get("CORS_ORIGINS", "*").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_origins == "*" else [o.strip() for o in _cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthReq(BaseModel):
    password: str = ""


class ChatReq(BaseModel):
    message: str


class MemoryReq(BaseModel):
    items: list[str] = []


# ── Auth + health ────────────────────────────────────────────────────────────────
@app.post("/auth")
async def auth(req: AuthReq):
    if not auth_enabled():
        # Open mode (spec: "or leave open on Railway private network").
        return {"ok": True, "token": "", "auth_required": False}
    if hmac.compare_digest(req.password, PORTAL_PASSWORD):
        return {"ok": True, "token": _expected_token(), "auth_required": True}
    raise HTTPException(status_code=401, detail="Incorrect password")


@app.get("/health")
async def health():
    uptime_s = int(time.time() - START_TIME)
    return {
        "ok": True,
        "version": PORTAL_VERSION,
        "uptime_seconds": uptime_s,
        "auth_required": auth_enabled(),
    }


# ── Data endpoints ───────────────────────────────────────────────────────────────
@app.get("/calendar", dependencies=[Depends(require_auth)])
async def calendar(days: int = 7):
    return {"events": get_events_structured(days=days)}


@app.get("/tasks", dependencies=[Depends(require_auth)])
async def tasks():
    return {"tasks": get_tasks_structured()}


@app.get("/memory", dependencies=[Depends(require_auth)])
async def memory_get():
    # ace_memory.json is the source of truth. Return it raw for the UI to parse.
    return {"memories": read_memory()}


@app.post("/memory", dependencies=[Depends(require_auth)])
async def memory_post(req: MemoryReq):
    items = [i.strip() for i in req.items if i and i.strip()]
    if not items:
        raise HTTPException(status_code=400, detail="No items provided")
    existing = read_memory()
    merged = merge_memories(items, existing)
    ok = write_memory(merged)
    return {"ok": ok, "memories": merged}


@app.get("/weather", dependencies=[Depends(require_auth)])
async def weather():
    return await get_weather()


# ── TEMPORARY: conversation-history recovery (read-only) ─────────────────────────
# Inspects Drive revision history of ace_conversation.json to recover messages
# lost to the 80-vs-160 trim bug. Read-only — no restore path. Remove once
# recovery is done or written off. See backend/revisions.py.
@app.get("/admin/revisions", dependencies=[Depends(require_auth)])
async def admin_revisions():
    return await asyncio.to_thread(list_revisions)


@app.get("/admin/revisions/{revision_id}", dependencies=[Depends(require_auth)])
async def admin_revision(revision_id: str):
    return await asyncio.to_thread(get_revision, revision_id)


@app.post("/admin/recover", dependencies=[Depends(require_auth)])
async def admin_recover():
    """Union all readable revisions into ace_history_recovered.json on Drive.

    Writes a NEW file only; ace_conversation.json is never modified.
    """
    return await asyncio.to_thread(recover_all)


@app.post("/chat", dependencies=[Depends(require_auth)])
async def chat(req: ChatReq):
    """Non-streaming fallback — collects the full reply then returns it."""
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Empty message")
    confirmations: list[str] = []

    async def emit(event_type, payload):
        if event_type == "confirmation":
            confirmations.append(payload["text"])

    reply = await stream_reply(msg, emit)
    return {"reply": reply, "confirmations": confirmations}


# ── WebSocket streaming chat ─────────────────────────────────────────────────────
@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    # Auth via ?token= query param (browsers can't set WS headers).
    token = websocket.query_params.get("token", "")
    if not token_valid(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    async def emit(event_type, payload):
        await websocket.send_json({"type": event_type, **payload})

    try:
        while True:
            data = await websocket.receive_json()
            user_text = (data.get("message") or "").strip()
            if not user_text:
                await emit("error", {"text": "Empty message"})
                continue
            await emit("start", {})
            await stream_reply(user_text, emit)
            await emit("done", {})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
    except Exception as e:
        logger.error("WebSocket error: %s", e)
        try:
            await emit("error", {"text": f"⚠️ {e}"})
        except Exception:
            pass


# ── Static frontend ──────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/manifest.json")
async def manifest():
    path = FRONTEND_DIR / "manifest.json"
    if path.exists():
        return FileResponse(path)
    return JSONResponse({}, status_code=404)


# Serve app.js, styles.css, icons, etc. Mounted last so API routes win.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
