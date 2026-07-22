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
import re
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
from .integrations.tasks_api import get_inbox_structured, get_task_lists_grouped, get_tasks_structured
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


_FILLERS = ("Mm —", "Okay —", "Right —", "So —", "Alright —", "Well —")
_last_filler = [""]


def _next_filler() -> str:
    """Short spoken lead-in for voice tool turns — varied, never the same one twice in a row."""
    import random
    choices = [f for f in _FILLERS if f != _last_filler[0]]
    pick = random.choice(choices)
    _last_filler[0] = pick
    return pick


# Soft continuers streamed if the voice SSE goes silent mid-turn (a slow tool await). The
# lead-in covers the FIRST-token deadline; this covers a long silent phase AFTER Ace has
# started, so a multi-second tool call (esp. an mcp_→Google round-trip on a screen handoff)
# never leaves dead air long enough for ElevenLabs to cut the call. Rotated so it never
# repeats the same word back-to-back.
_CONTINUERS = ("still on it", "one sec", "almost there", "bear with me", "hang tight", "just a moment")


def _continuer_cycler():
    """A per-call generator so each turn's continuers are ordered and don't repeat."""
    i = 0
    while True:
        yield _CONTINUERS[i % len(_CONTINUERS)]
        i += 1


# Adapter-injected speech (fillers, continuers, bail lines) must NEVER round-trip back into
# the model: ElevenLabs resends the whole conversation each request, so if Ace "sees himself"
# saying "Mm — one sec… almost there…" in the transcript he starts MIMICKING it (the babble
# spiral Brady hit). Strip our own noise from assistant history before it reaches the model.
_NOISE_LEAD = re.compile(r"^(?:(?:" + "|".join(re.escape(f) for f in _FILLERS) + r")\s+)+")
_NOISE_CONT = re.compile(
    r"(?:(?:" + "|".join(re.escape(c) for c in _CONTINUERS) + r")(?:…|\.\.\.)\s*)+", re.IGNORECASE)
_NOISE_LINES = (
    "I'm here — say that again for me?",
    "That one's hanging on me — try me again in a moment.",
    "Sorry — that took me a beat too long. Ask me again?",
    "Hit a snag on my end — give me a second and ask me again.",
)


def _strip_voice_noise(text: str) -> str:
    t = _NOISE_CONT.sub("", text)
    t = _NOISE_LEAD.sub("", t)
    for line in _NOISE_LINES:
        t = t.replace(line, "")
    return t.strip()


# What Ace SAYS while a tool runs on a live call — conversational, specific, first person.
# Falls back to the lowercased UI label for anything unlisted (incl. mcp_* names).
_SPOKEN_STATUS = {
    "get_calendar_range": "checking your calendar",
    "create_calendar_event": "putting it on your calendar",
    "delete_calendar_event": "pulling that off your calendar",
    "add_task": "adding that to your list",
    "complete_task": "checking that off",
    "send_email": "sending that email",
    "draft_email": "drafting that for you",
    "search_gmail": "going through your inbox",
    "read_gmail": "reading that email",
    "recall": "searching my memory",
    "search_drive": "digging through Drive",
    "save_memory": "locking that in",
    "capture_item": "noting that down",
    "update_item": "updating your bank",
}


# The single in-flight live-voice turn (one user, one call): a new /v1/chat/completions
# request cancels the previous turn's task so a retry/barge-in can't double-execute tools.
_active_voice_task = {"task": None}


async def publish_stage_event(event_type: str, payload: dict) -> int:
    """Broadcast a stage event to every connected HUD. Returns how many clients actually
    received it, so a voice handoff can tell whether any screen was there to build on."""
    dead = []
    sent = 0
    for ws in list(_stage_clients):
        try:
            await ws.send_json({"type": event_type, **payload})
            sent += 1
        except Exception:
            dead.append(ws)
    for ws in dead:
        _stage_clients.discard(ws)
    return sent


@app.on_event("startup")
async def _prime_voice_ctx():
    """Warm the voice context cache at boot + keep it warm, so the FIRST morning call is fast
    (no per-turn Google/Drive fetch on the critical path = no first-word timeout). Kicked as a
    BACKGROUND task so a slow Google call can't block startup and trip Railway's health check —
    a turn arriving before it finishes just falls back to an inline fetch (old behavior)."""
    async def _bg():
        try:
            await chat.prime_ctx()
        except Exception as e:
            logger.warning("voice ctx prime failed: %s", e)
    asyncio.create_task(_bg())

    async def _audit():
        # Log the LIVE ElevenLabs agent settings (voice, turn/soft-timeout, enabled tools)
        # so a drifted dashboard config shows up in the deploy logs, not just in a bad call.
        try:
            a = await voice.convai_agent_audit()
            logger.info("convai agent audit: %s", json.dumps(a)[:800])
        except Exception as e:
            logger.warning("convai agent audit failed: %s", e)
    asyncio.create_task(_audit())


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
    items, lists = await asyncio.gather(
        asyncio.to_thread(get_tasks_structured),
        asyncio.to_thread(get_task_lists_grouped),
    )
    return {"tasks": items, "lists": lists}


@app.get("/inbox", dependencies=[Depends(require_auth)])
async def inbox(n: int = 6):
    return {"emails": await asyncio.to_thread(get_inbox_structured, n)}


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

    # Heartbeat: during long turns (tools + thinking) the socket can sit silent for
    # 30-60s+ and the edge proxy drops it as idle — Brady sees it as "he timed out."
    # A ping every 20s keeps the line alive; the frontend ignores unknown event types.
    async def _keepalive():
        try:
            while True:
                await asyncio.sleep(20)
                await websocket.send_json({"type": "ping"})
        except Exception:
            pass

    ka = asyncio.create_task(_keepalive())

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
                # Seed from the UNIFIED thread (ace2's own log of BOTH voice + typed
                # turns), not the stale Telegram window — so typed Ace wakes up knowing
                # today's voice conversation and the correct day.
                seed = await asyncio.to_thread(chat._unified_thread)
                convo = sanitize_for_api(seed)
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
        ka.cancel()
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


@app.get("/thread", dependencies=[Depends(require_auth)])
async def thread_view(limit: int = 20):
    """The tail of Ace 2.0's own conversation (HUD + voice turns) for the chat panel,
    so Brady sees recent history on open without asking. Read-only, oldest-first."""
    limit = max(1, min(int(limit), 60))
    own = await asyncio.to_thread(history.read_recent, 2)
    tail = [
        {"role": m.get("role"), "text": (m.get("content") or "").strip(), "ts": m.get("ts")}
        for m in own[-limit:]
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    return {"messages": tail}


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
    # Assistant history is scrubbed of adapter noise (fillers/continuers/bail lines) so
    # the model never sees — and never mimics — its own keep-alive babble.
    prior = []
    for m in msgs:
        if m.get("role") not in ("user", "assistant"):
            continue
        t = _text(m.get("content", "")).strip()
        if m.get("role") == "assistant":
            t = _strip_voice_noise(t)
        if t:
            prior.append({"role": m["role"], "content": t})
    user_text = ""
    for m in reversed(prior):
        if m["role"] == "user":
            user_text = m["content"]
            break

    # ElevenLabs injects its enabled SYSTEM tools (end_call, skip_turn, …) into every
    # request in OpenAI format. Convert them to Anthropic passthrough schemas so Ace can
    # CALL them (goodnight → hang up; "hold on" → yield the turn); the actual execution
    # is ElevenLabs' — we just relay the call back as an OpenAI tool_call chunk below.
    # Nothing configured on the agent → empty list → behavior unchanged.
    el_tools = []
    _seen = {t["name"] for t in chat.VOICE_TOOLS}
    for t in (body.get("tools") or []):
        fn = (t or {}).get("function") or {}
        name = (fn.get("name") or "").strip()
        params = fn.get("parameters")
        if not name or name in _seen:
            continue
        _seen.add(name)
        if not isinstance(params, dict) or params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        el_tools.append({"name": name, "description": fn.get("description") or "",
                         "input_schema": params})

    created = int(time.time())
    # One line per voice request so a doubled/retried turn is provable in the logs
    # (there was no way to see ElevenLabs re-POSTs during the 8 AM incident).
    logger.info("voice turn req: prior=%d user=%r", len(prior), (user_text or "")[:80])

    async def sse():
        if not (user_text or "").strip():
            # ElevenLabs sometimes requests a turn with NO user message (call-opening
            # greeting / role-filtered request). Running a full turn against "" used to
            # make Ace SAY the words "Empty message". Greet and end instead.
            yield ("data: " + json.dumps({
                "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": "Right here."}, "finish_reason": None}],
            }) + "\n\n")
            yield ("data: " + json.dumps({
                "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }) + "\n\n")
            yield "data: [DONE]\n\n"
            return
        queue: asyncio.Queue = asyncio.Queue()
        # Context-aware in-between talk (Brady, 2026-07-19): NO upfront filler anymore —
        # a plain conversational turn streams Ace's actual words immediately, so "how's it
        # going" never gets "On it." theater. The filler now fires ONLY if the model's
        # first move is a tool call (text hasn't started), which also keeps ElevenLabs'
        # first-token deadline fed exactly when it's actually at risk (silent tool phase).
        spoke = {"any": False}
        status_said = set()   # tool names whose status word already played this turn

        async def emit(event_type, payload):
            if event_type == "delta":
                spoke["any"] = True
                await queue.put(("delta", payload.get("text", "")))
            elif event_type in ("final", "done"):
                await queue.put(("done", None))
            elif event_type == "error":
                # NEVER speak raw error text — an exception repr read aloud ("Error code:
                # 529 overloaded…") is pure gibberish to a listener AND poisons the resent
                # transcript for the rest of the call. Log the real error; say one fixed
                # human line (which the prior-scrubber also removes from history).
                logger.warning("voice turn error (spoken as snag line): %s", payload.get("text", ""))
                await queue.put(("delta", "Hit a snag on my end — give me a second and ask me again."))
                await queue.put(("done", None))
            elif event_type == "hold":
                # A gated action was blocked pending Brady's yes — nothing to say yet; the
                # model's confirmation question streams next, and the loop's lazy lead-in
                # covers any gap. (No status word — nothing happened.)
                pass
            elif event_type == "tool" and payload.get("status") == "running":
                # Screen-only tools (display_card/open_url) paint their own card — the card IS
                # the receipt, so no pill for them.
                if payload.get("ui"):
                    return
                # HUD RECEIPT: show a "◈ …" pill for EVERY real tool call, even in an 8-capture
                # sweep — so Brady can SEE each action land (voice used to show nothing on the
                # screen). The SPOKEN status below is still deduped so the audio isn't a chant.
                await publish_stage_event("tool", payload)
                name = payload.get("name")
                if name in status_said or len(status_said) >= 4:
                    return
                status_said.add(name)
                label = (payload.get("label") or "working on it").lower()
                spoken = _SPOKEN_STATUS.get(name, label)
                spoke["any"] = True
                await queue.put(("delta", f"{spoken.capitalize()}… "))
            elif event_type == "tool" and payload.get("status") == "done":
                # Flip the HUD pill to done (non-ui only; ui tools have no pill).
                if not payload.get("ui"):
                    await publish_stage_event("tool", payload)
            elif event_type == "confirmation":
                # The action receipt ("✅ Added to Deals: …") chat already shows — mirror it to
                # the HUD on voice too, so Brady sees proof the spoken action landed. Not spoken
                # (it's a screen receipt); Ace states the outcome himself in his reply.
                await publish_stage_event("confirmation", {"text": payload.get("text", "")})
            elif event_type in ("card", "open"):
                # Voice turn wants to paint the screen → push over the app WebSocket, not the
                # ElevenLabs audio stream. Same channel the typed path uses.
                await publish_stage_event(event_type, payload)
            elif event_type == "handoff":
                # Voice handed a Workspace AUTHORING task (Doc/Sheet/Slides/share link) to the
                # screen → tell the HUD to run it as a normal typed turn, which has the full MCP
                # toolset and the tested confirm flow. Same app WebSocket as the cards. Return
                # the reached-client count so the voice tool_result can be honest when no screen
                # is connected (don't promise "watch your screen" to a call with no HUD open).
                return await publish_stage_event("run_on_hud", {"message": payload.get("message", "")})
            elif event_type == "el_tool":
                # Ace called an ElevenLabs system tool (end_call / skip_turn) — relay it into
                # the SSE stream as an OpenAI tool_call so ElevenLabs performs the action.
                await queue.put(("tool_call", {
                    "id": payload.get("id") or f"call_{created}",
                    "type": "function",
                    "function": {"name": payload.get("name", ""),
                                 "arguments": json.dumps(payload.get("args") or {})},
                }))

        async def run():
            try:
                await chat.stream_turn(user_text, emit, prior=prior, fast=True, extra_tools=el_tools)
            finally:
                await queue.put(("done", None))

        # ONE live voice turn at a time: if ElevenLabs retried (its deadline missed, or the
        # call blipped) or Brady barged in, the PREVIOUS turn's task must not keep executing
        # tools and writing history behind the new one — that's the double-execution /
        # "Ace repeats himself" spiral. Single-user system: cancel-and-replace is correct.
        prev = _active_voice_task["task"]
        if prev is not None and not prev.done():
            logger.warning("voice turn superseded — cancelling previous in-flight turn")
            prev.cancel()
        task = asyncio.create_task(run())
        _active_voice_task["task"] = task
        # NO always-on lead-in anymore. With the pre-warmed context cache, Ace's real first
        # word arrives fast, so a plain turn streams his ACTUAL words — no "Mm—" noise every
        # turn (Brady's "hu mhh"). A lead-in is emitted LAZILY below only when the first token
        # is genuinely slow, as a backstop against ElevenLabs' first-token deadline.
        cont = _continuer_cycler()
        started = False   # has the turn produced ANY real output yet?
        sent_tool_call = False   # relayed an ElevenLabs system-tool call this turn?
        pre = 0           # 1.5s ticks waited BEFORE the first real token
        misses = 0        # consecutive 4s silences after start (resets on real output)
        conts_spoken = 0  # continuers voiced this TURN (never resets — hard babble budget)
        try:
            while True:
                try:
                    # Tight deadline BEFORE the first real token (catch a slow start before
                    # ElevenLabs cuts it); relaxed once the turn is actually streaming.
                    kind, text = await asyncio.wait_for(
                        queue.get(), timeout=(1.5 if not started else 4.0))
                except asyncio.TimeoutError:
                    if not started:
                        # First token is slow (a cold boot or model/API spike). Speak like a
                        # person waiting: ONE lead-in, a beat later ONE continuer, then another
                        # — never a chant (an uncapped 1.5s filler drumbeat is the "stuck in a
                        # loop" Brady heard). If it's STILL not started after ~13s, bail with
                        # one honest line and end the turn instead of babbling forever.
                        pre += 1
                        if pre == 1:
                            spoke["any"] = True
                            piece = _next_filler() + " "
                        elif pre in (3, 6):
                            piece = next(cont) + "… "
                        elif pre >= 9:
                            yield ("data: " + json.dumps({
                                "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{"index": 0, "delta": {"content": "Sorry — that took me a beat too long. Ask me again?"},
                                             "finish_reason": None}],
                            }) + "\n\n")
                            break
                        else:
                            continue   # in-between ticks: wait silently, no chatter
                        yield ("data: " + json.dumps({
                            "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                            "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                        }) + "\n\n")
                        continue
                    misses += 1
                    if misses > 8:
                        # ~30s+ of unbroken silence = a tool has truly hung. Don't stream filler
                        # forever (that keeps a dead call alive/billing); close out gracefully.
                        yield ("data: " + json.dumps({
                            "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                            "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"content": "That one's hanging on me — try me again in a moment."},
                                         "finish_reason": None}],
                        }) + "\n\n")
                        break
                    # Speak a continuer only every OTHER miss (≈8s apart) and at most 3 per
                    # turn — a hung tool gets a few human beats, then quiet until the cap,
                    # never a rotating chant that audibly wraps around.
                    if misses % 2 == 0 and conts_spoken < 3:
                        conts_spoken += 1
                        yield ("data: " + json.dumps({
                            "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                            "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"content": next(cont) + "… "},
                                         "finish_reason": None}],
                        }) + "\n\n")
                    continue
                started = True
                misses = 0
                if kind == "done":
                    break
                if kind == "tool_call":
                    # Relay the system-tool call in OpenAI streaming format; ElevenLabs
                    # executes it (hang up / yield the turn).
                    sent_tool_call = True
                    tc = dict(text)
                    tc["index"] = 0
                    yield ("data: " + json.dumps({
                        "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}],
                    }) + "\n\n")
                    continue
                chunk = {
                    "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            if not spoke["any"] and not sent_tool_call:
                # Nothing ever streamed (a blank model turn) → an empty completion makes
                # ElevenLabs treat the call as an LLM failure and cascade. Always say one line.
                yield ("data: " + json.dumps({
                    "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": "I'm here — say that again for me?"},
                                 "finish_reason": None}],
                }) + "\n\n")
            done_chunk = {
                "id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "tool_calls" if sent_tool_call else "stop"}],
            }
            yield f"data: {json.dumps(done_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            # Give the turn a short grace to finish persisting to history (a bail/hang path
            # may leave it a beat behind the stream) — a blind cancel left holes in the
            # RECENT THREAD that read as Ace "forgetting" the exchange.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except BaseException:
                pass
        finally:
            task.cancel()

    return StreamingResponse(sse(), media_type="text/event-stream")


# ── Static frontend (mounted last so API routes win) ────────────────────────────
@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
