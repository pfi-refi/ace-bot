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

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chat, daybank, history, voice
from .brain import (
    google_ready,
    read_memory,
    read_recovered_history,
    read_shared_conversation,
    sanitize_for_api,
)
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


def _allow_open() -> bool:
    # Local-dev escape hatch ONLY. Never set on Railway.
    return os.environ.get("ACE2_ALLOW_OPEN", "").strip() == "1"


def locked() -> bool:
    """No password configured -> the service is LOCKED, not open.

    Ace can send email and edit the calendar; an unset env var must never mean
    'public'. Brady unlocks by setting ACE2_PASSWORD in Railway.
    """
    return not _password() and not _allow_open()


def auth_enabled() -> bool:
    return bool(_password()) or locked()


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
    if locked():
        return False
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

# Live browser sockets (the HUD's /ws/chat). Single-user, so every connected socket is
# Brady's authenticated browser — we broadcast stage events (cards) to all of them. This is
# the side-channel that lets a VOICE turn (which rides ElevenLabs' pipe) paint the screen.
_stage_clients: set = set()
_bg_tasks: set = set()   # hold refs to fire-and-forget tasks so they aren't GC'd mid-run


async def publish_stage_event(event_type: str, payload: dict):
    dead = []
    for ws in list(_stage_clients):
        try:
            await ws.send_json({"type": event_type, **payload})
        except Exception:
            dead.append(ws)
    for ws in dead:
        _stage_clients.discard(ws)


class AuthReq(BaseModel):
    password: str = ""


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": VERSION,
        "uptime_seconds": int(time.time() - START_TIME),
        "auth_required": auth_enabled(),
        "locked": locked(),
    }


@app.post("/auth")
async def auth(req: AuthReq):
    if locked():
        raise HTTPException(status_code=503, detail="Locked — set ACE2_PASSWORD in Railway to unlock")
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


@app.get("/daybank", dependencies=[Depends(require_auth)])
async def daybank_read():
    return {"items": await asyncio.to_thread(daybank.read_items, True)}


class DaybankUpdateReq(BaseModel):
    id: str = ""
    status: str = ""  # "open" | "done"


@app.post("/daybank/update", dependencies=[Depends(require_auth)])
async def daybank_update(req: DaybankUpdateReq):
    """Toggle an item from the HUD checkbox. Mutates Ace's OWN private data-bank
    file only — never the shared conversation file. Same mutation Ace makes via the
    update_item tool, exposed directly so a checkbox is instant, not a chat round-trip."""
    status = req.status if req.status in ("open", "done") else None
    ok, _msg = await asyncio.to_thread(daybank.update_item, req.id, status)
    items = await asyncio.to_thread(daybank.read_items, True)
    return {"ok": ok, "items": items}


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


# ── Chat: streaming WebSocket + HTTP fallback ───────────────────────────────────
class ChatReq(BaseModel):
    message: str = ""


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """Real-time streaming chat. Token rides as ?token= (browsers can't set WS
    headers). A bad token closes 4401 so the client routes back to login."""
    token = websocket.query_params.get("token", "")
    if not token_valid(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _stage_clients.add(websocket)   # register for voice-turn stage events (cards)

    async def emit(event_type, payload):
        await websocket.send_json({"type": event_type, **payload})

    # Per-connection conversation: seeded once from the shared Telegram window
    # (read-only continuity), then grows with THIS session's turns — so Ace
    # remembers the conversation he's actually in, turn to turn.
    convo = None

    try:
        while True:
            data = await websocket.receive_json()
            text = (data.get("message") or "").strip()
            if not text:
                await emit("error", {"text": "Empty message"})
                continue
            if convo is None:
                shared = await asyncio.to_thread(read_shared_conversation)
                convo = sanitize_for_api(shared)
            convo.append({"role": "user", "content": text})
            await emit("start", {})
            reply = await chat.stream_turn(text, emit, prior=convo)
            if reply:
                convo.append({"role": "assistant", "content": reply})
            del convo[:-160]   # cap the window
            await emit("done", {})
    except WebSocketDisconnect:
        logger.info("WS disconnected.")
    except Exception as e:
        logger.error("WS error: %s", e)
        try:
            await emit("error", {"text": f"⚠️ {e}"})
        except Exception:
            pass
    finally:
        _stage_clients.discard(websocket)


@app.post("/chat", dependencies=[Depends(require_auth)])
async def chat_http(req: ChatReq):
    """Non-streaming fallback — collects the full reply + confirmations."""
    reply, confirmations = [], []

    async def emit(event_type, payload):
        if event_type == "final":
            reply.append(payload.get("text", ""))
        elif event_type == "confirmation":
            confirmations.append(payload.get("text", ""))
        elif event_type == "error":
            reply.append(payload.get("text", ""))

    await chat.stream_turn((req.message or "").strip(), emit)
    return {"reply": "".join(reply), "confirmations": confirmations}


# ── Voice: TTS proxy (ACE voice, key server-side) ───────────────────────────────
class TTSReq(BaseModel):
    text: str = ""


@app.post("/tts", dependencies=[Depends(require_auth)])
async def tts(req: TTSReq):
    audio, info = await voice.synthesize(req.text)
    if audio is None:
        # 204 → frontend falls back to browser speechSynthesis, no error noise.
        return Response(status_code=204, headers={"X-TTS-Fallback": info})
    return Response(content=audio, media_type=info)


@app.post("/stt", dependencies=[Depends(require_auth)])
async def stt(request: Request):
    """Transcribe recorded mic audio (raw body) → {text}. Real STT, not the browser's."""
    audio = await request.body()
    ctype = request.headers.get("content-type", "audio/webm")
    text, err = await voice.transcribe(audio, "speech.webm", ctype)
    return {"text": text or "", "error": err}


# ── Full-duplex realtime voice (ElevenLabs Agents) ──────────────────────────────
@app.get("/convai/config", dependencies=[Depends(require_auth)])
async def convai_config():
    on = voice.convai_enabled()
    return {"enabled": on, "agent_id": voice.CONVAI_AGENT_ID if on else None}


@app.get("/convai/signed-url", dependencies=[Depends(require_auth)])
async def convai_signed():
    url, err = await voice.convai_signed_url()
    return {"signed_url": url, "error": err}


# ── History: 2.0's own + the read-only shared/recovered Telegram past ────────────
@app.get("/history", dependencies=[Depends(require_auth)])
async def history_view(months: int = 3):
    """Merged transcript: 2.0's timestamped history + the shared Telegram window +
    the recovered pre-wipe archive. Read-only — nothing here can touch the bot."""
    own, shared, recovered = await asyncio.gather(
        asyncio.to_thread(history.read_recent, months),
        asyncio.to_thread(read_shared_conversation),
        asyncio.to_thread(read_recovered_history),
    )
    return {
        "own": own,                    # [{ts, source, role, content}]
        "shared": shared,              # [{role, content}] — current Telegram window
        "recovered_count": len(recovered),
    }


# ── Voice brain: OpenAI-compatible custom-LLM endpoint for ElevenLabs Agents ─────
# ElevenLabs' Conversational AI can use a custom LLM by POSTing an OpenAI-shaped
# /v1/chat/completions and streaming SSE back. This exposes Ace's REAL brain
# (same prompt, memory, tools) as that LLM — so a realtime voice agent is still
# the same Ace, tools and all, not a second assistant. Gated by ACE2_LLM_KEY.
def _llm_authorized(authorization: str) -> bool:
    want = os.environ.get("ACE2_LLM_KEY", "").strip()
    if not want:
        # Unset -> LOCKED (this endpoint drives Ace's real tools, incl. email).
        # Local dev can opt out with ACE2_ALLOW_OPEN=1.
        return _allow_open()
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    return hmac.compare_digest(token.strip(), want)


@app.post("/v1/chat/completions")
async def openai_compat(request: Request, authorization: str = Header(default="")):
    if not _llm_authorized(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    model = body.get("model", "ace-2")
    msgs = body.get("messages", [])

    def _text(c):
        return c if isinstance(c, str) else " ".join(
            b.get("text", "") for b in c if isinstance(b, dict)
        )

    # Full prior conversation (ElevenLabs sends it each call) → Ace's turn memory.
    prior = [
        {"role": m["role"], "content": _text(m.get("content", ""))}
        for m in msgs if m.get("role") in ("user", "assistant") and _text(m.get("content", "")).strip()
    ]
    user_text = ""
    for m in reversed(prior):
        if m["role"] == "user":
            user_text = m["content"]
            break

    created = int(time.time())

    async def sse():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event_type, payload):
            if event_type == "delta":
                await queue.put(("delta", payload.get("text", "")))
            elif event_type in ("final", "done"):
                await queue.put(("done", None))
            elif event_type == "error":
                await queue.put(("delta", payload.get("text", "")))
                await queue.put(("done", None))

        async def run():
            try:
                await chat.stream_turn(user_text, emit, prior=prior, fast=True)
            finally:
                await queue.put(("done", None))

        task = asyncio.create_task(run())
        try:
            while True:
                kind, text = await queue.get()
                if kind == "done":
                    break
                chunk = {
                    "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            done_chunk = {
                "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done_chunk)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            task.cancel()

    # Off to the side of the spoken reply: decide whether to paint a card on Brady's screen
    # and push it over the app WebSocket. Spawned OUTSIDE sse() so its lifecycle isn't tied to
    # the SSE `finally: task.cancel()`. Only runs when a browser is actually connected. It emits
    # ONLY card/open events (never delta/final), so it can't touch the voice first-token deadline.
    if _stage_clients:
        async def _stage_emit(event_type, payload):
            if event_type in ("card", "open"):
                await publish_stage_event(event_type, payload)
        _t = asyncio.create_task(chat.stage_pass(user_text, _stage_emit, prior=prior))
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)

    return StreamingResponse(sse(), media_type="text/event-stream")


# ── Static frontend (mounted last so API routes win) ────────────────────────────
@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
