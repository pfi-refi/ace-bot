"""
ACE 2.0 — JARVIS-style command center.

  GET  /health          — liveness + version + auth mode (unauthenticated)
  POST /auth            — exchange ACE2_PASSWORD for a signed, EXPIRING token
  GET  /session         — validate the caller's token (client calls this before
                          trusting a stored token; see the auth note below)
  GET  /calendar?days=  — structured events
  GET  /tasks           — structured tasks
  GET  /memory          — Ace's shared memory (source of truth)
  GET  /weather         — Cleveland conditions (key stays server-side)

Design rules carried in from the portal's scars:

1. ace_conversation.json is READ-ONLY here. See brain.py.
2. Every Google call is blocking, so it goes through asyncio.to_thread and the
   independent ones are gathered. The portal made six SEQUENTIAL blocking calls
   per turn inside an async handler, stalling the whole event loop — merely slow
   for text, fatal for realtime voice.
3. Tokens EXPIRE. The portal's token was hmac(secret, "ace-portal|" + password):
   a constant that never rotated, with no expiry and no revocation.
4. The client must VALIDATE a stored token before trusting it (GET /session).
   The portal trusted blind and ignored the WS 4401 close code, so a stale token
   produced a full UI with every panel dead and a 4s reconnect loop forever —
   with no route back to the login screen.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .brain import google_ready, read_memory
from .integrations.calendar_api import get_events_structured
from .integrations.tasks_api import get_tasks_structured
from .integrations.weather import get_weather

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s", level=logging.INFO
)
logger = logging.getLogger("ace2.main")

VERSION = "v2.0.0"
START_TIME = time.time()
FRONTEND_DIR = Path(__file__).resolve().parent.parent

TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days — long enough for an installed app
                                    # not to nag, short enough to actually rotate


# ── Auth ────────────────────────────────────────────────────────────────────────
def _password() -> str:
    return os.environ.get("ACE2_PASSWORD", "").strip()


def auth_enabled() -> bool:
    return bool(_password())


def _secret() -> bytes:
    raw = (
        os.environ.get("ACE2_TOKEN_SECRET")
        or os.environ.get("ANTHROPIC_API_KEY")
        or "ace2-dev-secret"
    )
    return hashlib.sha256(raw.encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token() -> str:
    """Signed token carrying its own expiry: base64(exp).signature."""
    exp = int(time.time()) + TOKEN_TTL_SECONDS
    payload = base64.urlsafe_b64encode(str(exp).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def token_valid(token: str) -> bool:
    if not auth_enabled():
        return True
    if not token or "." not in token:
        return False
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        pad = "=" * (-len(payload) % 4)
        exp = int(base64.urlsafe_b64decode(payload + pad).decode())
    except Exception:
        return False
    return time.time() < exp


async def require_auth(authorization: str = Header(default="")):
    if not auth_enabled():
        return
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token_valid(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(title="Ace 2.0", version=VERSION)


class AuthReq(BaseModel):
    password: str = ""


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": VERSION,
        "uptime_seconds": int(time.time() - START_TIME),
        "auth_required": auth_enabled(),
    }


@app.post("/auth")
async def auth(req: AuthReq):
    if not auth_enabled():
        return {"ok": True, "token": "", "auth_required": False}
    if not hmac.compare_digest((req.password or "").strip(), _password()):
        raise HTTPException(status_code=401, detail="Access denied")
    return {"ok": True, "token": issue_token(), "auth_required": True}


@app.get("/session", dependencies=[Depends(require_auth)])
async def session():
    """Token check. The client calls this before revealing the UI, so a stale or
    expired token routes to the login screen instead of a dead shell."""
    return {"ok": True}


# ── Data ────────────────────────────────────────────────────────────────────────
@app.get("/calendar", dependencies=[Depends(require_auth)])
async def calendar(days: int = 7):
    events = await asyncio.to_thread(get_events_structured, days)
    return {"events": events}


@app.get("/tasks", dependencies=[Depends(require_auth)])
async def tasks():
    items = await asyncio.to_thread(get_tasks_structured)
    return {"tasks": items}


@app.get("/memory", dependencies=[Depends(require_auth)])
async def memory():
    return {"memories": await asyncio.to_thread(read_memory)}


@app.get("/weather", dependencies=[Depends(require_auth)])
async def weather():
    return await get_weather()  # already async (httpx)


@app.get("/bootstrap", dependencies=[Depends(require_auth)])
async def bootstrap(days: int = 7):
    """Every panel's data in ONE round trip, fetched concurrently.

    The HUD would otherwise fire four requests on boot, each blocking a thread on
    sequential Google I/O. return_exceptions keeps one dead integration from
    taking the whole dashboard down — each panel degrades on its own.
    """
    events, items, mem, wx = await asyncio.gather(
        asyncio.to_thread(get_events_structured, days),
        asyncio.to_thread(get_tasks_structured),
        asyncio.to_thread(read_memory),
        get_weather(),
        return_exceptions=True,
    )

    # A service is only "up" if creds exist AND the call didn't raise. The
    # integrations swallow their own errors and return [], so an exception is
    # not a reliable failure signal on its own — without the google_ready()
    # gate every dot reports green while nothing works.
    g_ok = await asyncio.to_thread(google_ready)

    def ok(v, default, needs_google=True):
        if isinstance(v, Exception):
            logger.warning("bootstrap: %s", v)
            return default, False
        return v, (g_ok if needs_google else True)

    events, e_ok = ok(events, [])
    items, t_ok = ok(items, [])
    mem, m_ok = ok(mem, [])
    wx, w_ok = ok(wx, {"ok": False, "condition": "UNAVAILABLE"}, needs_google=False)
    if not isinstance(wx, Exception):
        w_ok = bool(wx.get("ok"))  # weather reports its own health honestly
    return {
        "events": events,
        "tasks": items,
        "memories": mem,
        "weather": wx,
        "services": {"calendar": e_ok, "tasks": t_ok, "drive": m_ok, "weather": w_ok},
    }


# ── Static frontend (mounted last so API routes win) ────────────────────────────
@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
