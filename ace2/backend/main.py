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
import threading
import time
from datetime import datetime
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

from . import chat, daybank, db, history, memory_db, voice
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


@app.get("/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    """Client reads these on load to sync toggle state (e.g. Discreet Mode)."""
    return {"discreet": chat._discreet[0]}


class DiscreetReq(BaseModel):
    on: bool = False


@app.post("/settings/discreet", dependencies=[Depends(require_auth)])
async def set_discreet(req: DiscreetReq):
    """DISCREET MODE: when on, Ace won't speak dollar figures aloud (offers to read them or
    show them on screen) and phone-brief pushes go generic. Persisted across restarts."""
    return {"discreet": await asyncio.to_thread(chat.set_discreet, req.on)}


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


@app.get("/memory/facts", dependencies=[Depends(require_auth)])
async def memory_facts():
    """Full fact rows (id, text, tier, ts, invalid_at) for curation/review. Postgres only."""
    if not db.enabled():
        return {"facts": [], "postgres": False}
    return {"facts": await asyncio.to_thread(db.read_facts_full), "postgres": True}


@app.get("/memory/search", dependencies=[Depends(require_auth)])
async def memory_search(q: str = "", limit: int = 10):
    """SEMANTIC / HYBRID memory search — the ranked rows behind Ace's `recall` tool.

    Postgres full-text (stemmed english, so paraphrases hit) fused by Reciprocal Rank
    Fusion with pg_trgm fuzzy matching (typos, partial names) across durable facts,
    conversation turns, and the task board. Here for testing and for a future
    search-the-memory UI; `text` mirrors exactly what Ace himself would read.
    """
    q = (q or "").strip()
    if not q:
        return {"query": "", "count": 0, "hits": [], "postgres": db.enabled()}
    n = max(1, min(int(limit or 10), 50))
    if not db.enabled():
        # Postgres dormant → no ranked rows exist, but recall still works (Drive corpus).
        return {"query": q, "count": 0, "hits": [], "postgres": False,
                "text": await asyncio.to_thread(memory_db.recall, q, min(n, 20))}
    hits = await asyncio.to_thread(db.hybrid_search, q, n)
    return {"query": q, "count": len(hits), "hits": hits, "postgres": True}


class CurateReq(BaseModel):
    archive: list = []   # substrings — any LIVE fact containing one is archived (kept as history)
    core: list = []      # substrings — matching facts promoted to tier 'core' (always pinned)
    add: list = []       # [{"text":..., "tier":"core|active"}] new facts to add


@app.post("/memory/sweep", dependencies=[Depends(require_auth)])
async def memory_sweep():
    """Run the learning + triage sweep NOW (extract new facts to memory + route actionable
    to-dos from recent conversation into the right Google Tasks list). Returns what it did."""
    return await chat._learn_sweep_once(force=True)


@app.post("/memory/curate", dependencies=[Depends(require_auth)])
async def memory_curate(req: CurateReq):
    """Curate durable facts: archive-not-delete stale ones, pin core ones, add fresh ones.
    Archiving sets tier='archived' + invalid_at (searchable history, never deleted)."""
    if not db.enabled():
        raise HTTPException(status_code=400, detail="Postgres brain not enabled")

    def _run():
        facts = db.read_facts_full()
        archived, cored = [], []
        arch = [s.lower() for s in (req.archive or []) if s]
        core = [s.lower() for s in (req.core or []) if s]
        for f in facts:
            low = (f.get("text") or "").lower()
            if f.get("invalid_at"):
                continue  # already archived
            if arch and any(s in low for s in arch):
                if db.archive_fact(f["id"]):
                    archived.append(f["text"])
            elif core and any(s in low for s in core):
                if db.set_fact_tier(f["id"], "core"):
                    cored.append(f["text"])
        added = []
        for a in (req.add or []):
            txt = (a.get("text") if isinstance(a, dict) else str(a)) or ""
            tier = (a.get("tier") if isinstance(a, dict) else "active") or "active"
            if txt.strip() and db.add_fact(txt.strip(), tier=tier):
                added.append(txt.strip())
        return {"archived": archived, "cored": cored, "added": added,
                "current_count": len(db.read_facts())}

    return await asyncio.to_thread(_run)


@app.get("/weather", dependencies=[Depends(require_auth)])
async def weather():
    return await get_weather()  # already async (httpx)


@app.get("/daybank", dependencies=[Depends(require_auth)])
async def daybank_read(all: bool = False):
    # all=true also returns done items across days (for the panel's "Done" lens); default is the
    # active view (open items + anything touched today).
    return {"items": await asyncio.to_thread(daybank.read_items, not all)}


class DaybankUpdateReq(BaseModel):
    id: str = ""
    status: str = ""     # "open" | "done" | "" (no status change)
    text: str = ""       # new wording ("" = leave)
    category: str = ""   # move to another board column ("" = leave)
    due: str | None = None   # new due ("" clears it; None = leave)


class DaybankAddReq(BaseModel):
    text: str = ""
    category: str = ""   # Deals | Agents | Admin | Networking | Business | Tech


@app.post("/daybank/add", dependencies=[Depends(require_auth)])
async def daybank_add(req: DaybankAddReq):
    """Add a task from the Command panel into Ace's OWN store (deduped against open AND done, so
    no twins and no resurrection). Category rides as a tag for grouping/filtering."""
    tags = [req.category] if (req.category or "").strip() else None
    ok, res = await asyncio.to_thread(daybank.add_item, "todo", req.text, None, tags)
    dup = bool(isinstance(res, dict) and res.get("dup"))
    items = await asyncio.to_thread(daybank.read_items, True)
    return {"ok": ok, "dup": dup, "items": items}


class MigrateReq(BaseModel):
    dry_run: bool = True


@app.post("/daybank/migrate", dependencies=[Depends(require_auth)])
async def daybank_migrate(req: MigrateReq):
    """One-time migration: reconcile Brady's BUSINESS Google Tasks against what's already in Ace's
    store and import only what's genuinely MISSING — treating reworded/paraphrased versions as
    duplicates so nothing double-adds. ADDITIVE ONLY: never deletes anything from Google Tasks.
    dry_run=true (default) reports what WOULD import without writing a thing."""
    from .integrations.tasks_api import get_task_lists_grouped
    from . import chat
    SKIP = ("personal", "goal", "no touch")   # these stay in Google Tasks
    grouped = await asyncio.to_thread(get_task_lists_grouped)
    g_items = []
    for l in grouped:
        name = (l.get("list") or "")
        if any(s in name.lower() for s in SKIP):
            continue
        for t in l.get("tasks", []):
            title = (t.get("title") or "").strip()
            if title:
                g_items.append(f"[{name}] {title}")
    existing = await asyncio.to_thread(daybank.read_items, False)
    have = "\n".join(f"- {it.get('text', '')}" for it in existing)
    gtxt = "\n".join(f"- {g}" for g in g_items)
    client = chat._anthropic()
    resp = await client.messages.create(
        model=chat.LEARN_MODEL, max_tokens=2500,
        messages=[{"role": "user", "content": (
            "Migrate Brady's business tasks from Google Tasks into Ace's store. Two lists follow: "
            "what's ALREADY IN ACE, and what's in GOOGLE (business lists). Return ONLY the Google "
            "tasks that are genuinely NOT already represented in Ace. CRITICAL: treat reworded, "
            "paraphrased, or shorthand versions of the same task as DUPLICATES and DROP them — both "
            "against Ace's list AND against each other (Google itself has reworded dupes). When "
            "unsure whether two refer to the same real task, treat them as the SAME (drop it) rather "
            "than risk a double-add. Assign each kept task a CATEGORY from: Deals, Agents, Admin, "
            "Networking, Business, Tech. One per line, EXACTLY: CATEGORY :: task title. If every "
            "Google task is already covered, reply NONE.\n\n"
            "ALREADY IN ACE:\n" + (have or "(none)") + "\n\n"
            "IN GOOGLE (business lists):\n" + (gtxt or "(none)"))}])
    out = "".join(getattr(b, "text", "") for b in resp.content).strip()
    proposed = []
    for ln in out.split("\n"):
        if "::" not in ln:
            continue
        cat, title = ln.split("::", 1)
        cat, title = cat.strip("-•* ").strip(), title.strip()
        if len(title) > 3:
            proposed.append({"category": cat, "title": title})
    imported = 0
    if not req.dry_run:
        for p in proposed:
            ok, res = await asyncio.to_thread(
                daybank.add_item, "todo", p["title"], None, [p["category"], "migrated"])
            if ok and not (isinstance(res, dict) and res.get("dup")):
                imported += 1
    return {
        "dry_run": req.dry_run,
        "google_business_tasks": len(g_items),
        "already_in_ace": len(existing),
        "proposed_new": proposed,
        "proposed_count": len(proposed),
        "imported": imported,
    }


@app.post("/tasks/clear_all", dependencies=[Depends(require_auth)])
async def tasks_clear_all(req: MigrateReq):
    """GOOGLE TASKS RETIREMENT (Brady's explicit call): complete every open task in every
    list except NO TOUCH. Complete-not-delete — recoverable in Google's completed view.
    Run only after the board holds everything (it does: migrated + imported + deduped)."""
    from .integrations.tasks_api import complete_all_tasks
    return await asyncio.to_thread(complete_all_tasks, req.dry_run)


@app.post("/brief/run", dependencies=[Depends(require_auth)])
async def brief_run(kind: str = "morning"):
    """Generate a brief NOW (also used by the scheduled loops): 'morning' = the wake-up
    game plan + business snapshot; 'eod' = the evening recap. Lands in the conversation
    thread and pushes to any open HUD."""
    kind = kind if kind in ("morning", "eod") else "morning"
    text = await chat.generate_brief(kind)   # deliver_brief inside handles thread+push+HUD
    if text:
        return {"ok": True, "kind": kind, "chars": len(text)}
    return {"ok": False, "error": "brief generation failed"}


@app.post("/watch/run", dependencies=[Depends(require_auth)])
async def watch_run(force: bool = False, dry_run: bool = False):
    """Run ONE ambient watch pass now and report what it decided — the test hook for the loop
    that otherwise runs itself every ~12 min. force ignores the quiet-hours and rate-limit
    gates so a pass can be exercised at any hour; dry_run decides without delivering or
    consuming the diff state, so the same signals are still live for the real loop."""
    return await chat._watch_pass_once(force=force, dry_run=dry_run)


@app.post("/reminder/run", dependencies=[Depends(require_auth)])
async def reminder_run(force: bool = False, dry_run: bool = False):
    """Fire ONE deterministic due-soon reminder now (test hook for the afternoon loop). force
    skips the time-window + daily-claim gates; dry_run builds the message without delivering."""
    return await chat._reminder_once(force=force, dry_run=dry_run)


@app.post("/daybank/import_personal_goals", dependencies=[Depends(require_auth)])
async def daybank_import_personal_goals(req: MigrateReq):
    """One-time: bring Brady's Google PERSONAL + GOALS lists into Ace's store (tagged Personal /
    Goals) so EVERYTHING lives in Ace. Deterministic — no LLM; add_item's dedup blocks twins.
    Additive only; Google is never modified."""
    from .integrations.tasks_api import get_task_lists_grouped
    grouped = await asyncio.to_thread(get_task_lists_grouped)
    proposed, imported = [], 0
    for l in grouped:
        low = (l.get("list") or "").lower()
        if "no touch" in low or not ("personal" in low or "goal" in low):
            continue
        cat = "Goals" if "goal" in low else "Personal"
        for t in l.get("tasks", []):
            title = (t.get("title") or "").strip()
            if title:
                proposed.append({"category": cat, "title": title})
    if not req.dry_run:
        for p in proposed:
            ok, res = await asyncio.to_thread(
                daybank.add_item, "todo", p["title"], None, [p["category"], "migrated"])
            if ok and not (isinstance(res, dict) and res.get("dup")):
                imported += 1
    return {"dry_run": req.dry_run, "proposed_count": len(proposed),
            "proposed": proposed, "imported": imported}


@app.post("/daybank/categorize", dependencies=[Depends(require_auth)])
async def daybank_categorize():
    """One-time backfill: give a category tag to any open item that lacks one, so the Command
    panel's Pipeline view groups everything correctly."""
    import re
    from . import chat, db as _db
    CATS = set(db.CATEGORIES)
    items = await asyncio.to_thread(daybank.read_items, False)
    untagged = [it for it in items if it.get("status") != "done"
                and not any(t in CATS for t in (it.get("tags") or []))]
    if not untagged:
        return {"categorized": 0, "of": 0}
    numbered = "\n".join(f"{i}. {it.get('text', '')}" for i, it in enumerate(untagged))
    client = chat._anthropic()
    resp = await client.messages.create(
        model=chat.LEARN_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": (
            "Assign each of Brady's tasks a single CATEGORY from exactly: "
            + ", ".join(db.CATEGORIES)
            + ". Reply one per line, EXACTLY: <number>. <Category>\n\n"
            + numbered)}])
    out = "".join(getattr(b, "text", "") for b in resp.content)
    _CANON = {c.lower(): c for c in db.CATEGORIES}
    done = 0
    for ln in out.split("\n"):
        m = re.match(r"\s*(\d+)\.\s*(.+?)\s*$", ln)   # capture the rest, so 'Job Hunt' survives
        if not m:
            continue
        idx = int(m.group(1))
        cat = _CANON.get(m.group(2).strip().lower())   # case/space-insensitive → canonical
        if cat and 0 <= idx < len(untagged) and cat in CATS:
            it = untagged[idx]
            newtags = [t for t in (it.get("tags") or []) if t not in CATS] + [cat]
            if await asyncio.to_thread(_db.set_item_tags, it["id"], newtags):
                done += 1
    return {"categorized": done, "of": len(untagged)}


@app.post("/daybank/update", dependencies=[Depends(require_auth)])
async def daybank_update(req: DaybankUpdateReq):
    """Edit a board item from the Command panel: toggle done, rewrite the text, move it
    to another category, or change its due — instant, no chat round-trip. Mutates Ace's
    OWN store only."""
    status = req.status if req.status in ("open", "done", "dropped") else None
    text = req.text.strip() or None
    tags = None
    _CATS = set(db.CATEGORIES)
    if req.category and req.category in _CATS:
        it = next((x for x in await asyncio.to_thread(daybank.read_items, False)
                   if x.get("id") == req.id), None)
        keep = [t for t in ((it.get("tags") if it else None) or []) if t not in _CATS]
        tags = [req.category] + keep
    ok, _msg = await asyncio.to_thread(
        daybank.update_item, req.id, status, text, tags, req.due)
    # REMEMBER THE WINS: completing a Deal or a Goal logs a durable memory note so Ace tracks
    # accomplishments over time — not every checkbox, only the meaningful categories.
    if ok and status == "done":
        try:
            from . import brain
            from datetime import datetime as _dt
            import pytz as _pytz
            it = next((x for x in await asyncio.to_thread(daybank.read_items, False)
                       if x.get("id") == req.id), None)
            cats = set(it.get("tags") or []) if it else set()
            if cats & {"Deals", "Goals"}:
                today = _dt.now(_pytz.timezone("America/New_York")).strftime("%B %-d, %Y")
                kind = "Goal reached" if "Goals" in cats else "Deal won"
                await asyncio.to_thread(
                    brain.add_memory, [f"{kind}: {it.get('text', '')} — {today}."], "win")
        except Exception as e:
            logger.warning("win-logging failed: %s", e)
    items = await asyncio.to_thread(daybank.read_items, True)
    return {"ok": ok, "items": items}


# ── THE KNOWLEDGE GRAPH — Brady's book of business as a navigable map ───────────
# ONE LEARN_MODEL pass reads the durable facts + the open board and returns the
# entity graph behind them: people (agents/prospects/clients) ↔ deals ↔ the board's
# categories. That call is slow and costs money, so the result is CACHED in the
# summaries table under kind='graph_cache' and only rebuilt when it ages past
# GRAPH_TTL (or ?refresh=1) — the panel opens instantly on every other visit.
#
# Parsing is DEFENSIVE on purpose: the model occasionally wraps its JSON in a
# sentence of prose, so we take the outermost {...} and then validate every node
# and edge. Anything malformed is dropped rather than shipped to the canvas —
# a half-built graph still renders; a thrown exception is a black screen.
GRAPH_TTL = 26 * 3600   # a day + margin — the warm loop rebuilds daily, so a cached read
                        # is served instantly all day and a tap never triggers a live rebuild
GRAPH_TYPES = ("person", "deal", "category")


def _graph_json(text: str) -> dict:
    """Outermost {...} out of a model reply, parsed. {} when there's nothing usable.

    Tolerates TRUNCATION: a graph of this size can run past max_tokens mid-array, which
    strict json.loads rejects outright — losing every node the model did produce. So on
    failure we salvage: close the dangling string/array/object and retry, and failing
    that, harvest whatever complete {...} objects exist into nodes/edges by shape."""
    if not text:
        return {}
    i = text.find("{")
    if i < 0:
        return {}
    j = text.rfind("}")
    if j > i:
        try:
            out = json.loads(text[i:j + 1])
            if isinstance(out, dict):
                return out
        except Exception:
            pass
    # --- salvage a truncated reply ---
    frag = text[i:]
    trimmed = frag[:frag.rfind("}") + 1] if "}" in frag else frag
    for tail in ('"}]}', '}]}', ']}', '}}', '}'):
        try:
            out = json.loads(trimmed + tail)
            if isinstance(out, dict) and (out.get("nodes") or out.get("edges")):
                logger.info("graph: recovered a truncated reply")
                return out
        except Exception:
            continue
    nodes, edges = [], []
    for m in re.finditer(r"\{[^{}]*\}", frag):
        try:
            o = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(o, dict):
            if o.get("label"):
                nodes.append(o)
            elif o.get("source") and o.get("target"):
                edges.append(o)
    if nodes:
        logger.info("graph: harvested %d node(s) / %d edge(s) from a broken reply",
                    len(nodes), len(edges))
        return {"nodes": nodes, "edges": edges}
    return {}


def _graph_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")


def _graph_shape(raw: dict) -> dict:
    """Normalize the model's entity dump into {nodes, edges} the canvas can trust:
    slugged ids, known types, no orphan edges, no duplicates. Node size is computed
    from DEGREE here (not asked of the model) so the map's visual weight is honest —
    the busiest person/deal is literally the biggest dot."""
    nodes, index = [], {}
    for n in (raw.get("nodes") or [])[:240]:
        if not isinstance(n, dict):
            continue
        label = str(n.get("label") or n.get("id") or "").strip()
        nid = _graph_key(label)
        if not nid or nid in index:
            continue
        kind = str(n.get("type") or "person").strip().lower()
        index[nid] = {"id": nid, "label": label[:52],
                      "type": kind if kind in GRAPH_TYPES else "person", "size": 8}
        nodes.append(index[nid])
    edges, seen, degree = [], set(), {}
    for e in (raw.get("edges") or [])[:600]:
        if not isinstance(e, dict):
            continue
        s, t = _graph_key(str(e.get("source") or "")), _graph_key(str(e.get("target") or ""))
        if s == t or s not in index or t not in index:
            continue   # orphan edge — the model named something it never declared
        pair = (s, t) if s < t else (t, s)
        if pair in seen:
            continue
        seen.add(pair)
        edges.append({"source": s, "target": t,
                      "kind": str(e.get("kind") or "linked_to").strip()[:28] or "linked_to"})
        degree[s] = degree.get(s, 0) + 1
        degree[t] = degree.get(t, 0) + 1
    for nd in nodes:
        nd["size"] = round(7 + 1.7 * min(degree.get(nd["id"], 0), 11), 1)
    return {"nodes": nodes, "edges": edges}


@app.get("/graph", dependencies=[Depends(require_auth)])
async def graph(refresh: int = 0):
    """The knowledge graph: every person, deal and category Ace knows about, and how
    they connect. Cached ~6h — pass ?refresh=1 to force a fresh extraction."""
    # Always read the cache, even on ?refresh=1 — a failed rebuild falls back to it below.
    cached = await asyncio.to_thread(db.latest_summary, "graph_cache") if db.enabled() else {}
    if cached.get("text") and not refresh:
        try:
            age = time.time() - datetime.fromisoformat(cached["ts"]).timestamp()
        except Exception:
            age = GRAPH_TTL + 1   # unreadable stamp → treat as stale, rebuild
        if age < GRAPH_TTL:
            out = _graph_json(cached["text"])
            if out.get("nodes"):
                return {**out, "cached": True, "age_seconds": int(age)}

    facts = await asyncio.to_thread(db.read_facts_full) if db.enabled() else []
    live = [f["text"] for f in facts if not f.get("invalid_at")][:220]
    if not live:
        live = (await asyncio.to_thread(read_memory))[:220]
    items = await asyncio.to_thread(daybank.read_items, True)
    board = [it for it in items if it.get("status") != "done"][:120]
    if not live and not board:
        return {"nodes": [], "edges": [], "cached": False, "empty": True}

    lines = "\n".join(f"- {t}" for t in live)
    tasks = "\n".join(
        "- [{}] {}".format(", ".join(it.get("tags") or []) or "Admin", it.get("text", ""))
        for it in board)
    prompt = (
        "You are Ace's KNOWLEDGE GRAPH builder for Brady McGraw, who runs Platinum "
        "Fortune Impact — a real-estate / life-insurance / refi base shop with ~18 agents. "
        "From his memory facts and his open board below, extract the ENTITY GRAPH of his "
        "book of business.\n\n"
        "NODES — one per real entity, no duplicates, and use the person's/deal's real name:\n"
        '  type "person"   — an agent, prospect, client, referral partner or family member\n'
        '  type "deal"     — a specific transaction or policy (e.g. "Kiana Wiggins deal", '
        '"Miller refi")\n'
        '  type "category" — ONLY these board columns, and only when something connects to '
        "them: " + ", ".join(db.CATEGORIES) + "\n\n"
        "EDGES — how they actually connect. `kind` is a short snake_case verb read "
        "source→target, e.g. deal_of, works_with, recruited_by, referred_by, client_of, "
        "married_to, owns, belongs_to. Example: {\"source\":\"Kiana Wiggins deal\","
        "\"target\":\"Corrine\",\"kind\":\"deal_of\"}. Connect every person and deal to the "
        "board category it belongs to, and connect people to each other wherever a fact says "
        "they are related, on the same deal, or on the same team.\n\n"
        "Rules: Brady himself is a node. Skip abstractions, dates and to-do phrasing — only "
        "named people, named deals, and the eight categories. HARD CAP: at most 55 nodes and "
        "90 edges — pick the most connected, most active entities and drop the rest. Keep "
        "labels under 30 characters. Every edge's source and target MUST exactly match a "
        "node label.\n\n"
        "Reply with STRICT JSON and nothing else:\n"
        '{"nodes":[{"id":"kiana-wiggins","label":"Kiana Wiggins","type":"person"}],'
        '"edges":[{"source":"Kiana Wiggins","target":"Deals","kind":"belongs_to"}]}\n\n'
        f"MEMORY FACTS:\n{lines or '(none)'}\n\nOPEN BOARD (category in brackets):\n"
        f"{tasks or '(none)'}")
    out = {"nodes": []}
    try:
        client = chat._anthropic()
        resp = await client.messages.create(
            model=chat.LEARN_MODEL, max_tokens=16000,
            messages=[{"role": "user", "content": prompt}])
        out = _graph_shape(_graph_json("".join(getattr(b, "text", "") for b in resp.content)))
    except Exception as e:
        logger.warning("graph extraction failed: %s", e)
        out = {"nodes": [], "_err": f"{type(e).__name__}: {e}"[:200]}
    if not out["nodes"]:
        # Extraction failed or came back unusable — an OLD map beats a blank screen, so
        # serve the stale cache (flagged) instead of nothing.
        stale = _graph_json(cached.get("text") or "")
        if stale.get("nodes"):
            return {**stale, "cached": True, "stale": True}
        return {"nodes": [], "edges": [], "cached": False,
                "error": out.get("_err") or "extraction produced no usable nodes"}
    out["generated_at"] = datetime.now(db.EASTERN).isoformat()
    if db.enabled():
        await asyncio.to_thread(db.add_summary, json.dumps(out), "graph_cache")
    logger.info("graph: %d nodes / %d edges built", len(out["nodes"]), len(out["edges"]))
    return {**out, "cached": False}


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
class VoiceSetReq(BaseModel):
    voice_id: str = ""


@app.post("/voice/set", dependencies=[Depends(require_auth)])
async def voice_set(req: VoiceSetReq):
    """Swap Ace's voice EVERYWHERE in one call: patches the live ElevenLabs agent and
    stores the TTS override. Paste a Voice ID from the ElevenLabs voice library."""
    return await voice.set_agent_voice(req.voice_id)


@app.post("/business/report", dependencies=[Depends(require_auth)])
async def business_report():
    """On-demand 'how's my business looking?' — the deep pipeline/team/goals read.
    Lands in the thread and returns the text for instant display."""
    text = await chat.generate_business_report()
    return {"ok": bool(text), "text": text}


class BriefFeedbackReq(BaseModel):
    kind: str = "morning"
    vote: str = "up"   # up | down


@app.post("/brief/feedback", dependencies=[Depends(require_auth)])
async def brief_feedback(req: BriefFeedbackReq):
    """👍/👎 on a brief — steers tomorrow's tone and tightness."""
    vote = "up" if req.vote != "down" else "down"
    ok = await asyncio.to_thread(db.add_summary, f"{req.kind}:{vote}", "brief_feedback")
    return {"ok": ok}


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


# ── CAPTURE ANYTHING: photo / PDF / voice memo → facts + to-dos, filed automatically ──
# Brady shoots a whiteboard, drops a statement, or mumbles a memo; Ace reads or hears it,
# extracts what matters, and FILES it (durable facts → memory, actions → the board) instead
# of leaving it to rot in his camera roll. One endpoint, three input shapes.
#
# The multipart body is parsed with the stdlib `email` parser ON PURPOSE. FastAPI's
# UploadFile/File() needs python-multipart, which is NOT in requirements — declaring one
# would raise at import time and take the WHOLE service down on deploy. Stdlib keeps this
# endpoint strictly additive: it cannot break a single existing route.
CAPTURE_MAX_BYTES = 18 * 1024 * 1024          # ~18MB — anything bigger is a video, not a capture
CAPTURE_IMAGE_MAX_BYTES = 5 * 1024 * 1024     # Anthropic's per-image ceiling; the UI downscales first
CAPTURE_CATEGORIES = db.CATEGORIES
CAPTURE_IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")

_CAPTURE_ASK = (
    "You are Ace, Brady McGraw's chief of staff at Platinum Fortune Impact (life insurance / "
    "agency building). Read EVERYTHING in this capture — handwriting included: names, phone "
    "numbers, emails, dollar figures, carriers, dates, times, scribbles in the margin.\n\n"
    "Return ONLY strict JSON. No prose, no markdown fence, nothing outside the object:\n"
    '{"summary": "2-4 plain sentences: what this is and what you took from it",\n'
    ' "facts": ["durable things worth remembering forever, one per string"],\n'
    ' "todos": [{"text": "an action Brady must take, phrased as an action", "category": "'
    + "|".join(CAPTURE_CATEGORIES) + '"}],\n'
    ' "contacts": [{"name": "", "phone": "", "email": "", "note": ""}],\n'
    ' "text": "the full raw text you read, verbatim"}\n\n'
    "FACTS stay true (who someone is, a policy amount, an address, a carrier's rule). TODOS "
    "must HAPPEN (call X, send Y, follow up Thursday) — keep any date/time inside the todo "
    "text so it isn't lost. Empty arrays are fine and better than filler. NEVER invent "
    "anything that isn't in the capture."
)


def _capture_part(body: bytes, content_type: str):
    """Pull the first FILE part out of a multipart/form-data body → (filename, ctype, bytes).

    Stdlib `email` handles binary payloads correctly (surrogateescape round-trip, CRLF
    preserved), so no dependency is needed. Returns (None, None, None) if there's no file.
    """
    from email.parser import BytesParser
    from email.policy import default as email_policy

    header = ("Content-Type: " + (content_type or "") + "\r\nMIME-Version: 1.0\r\n\r\n")
    msg = BytesParser(policy=email_policy).parsebytes(header.encode("utf-8", "replace") + body)
    if not msg.is_multipart():
        return None, None, None
    for part in msg.iter_parts():
        name = part.get_filename()
        if not name:
            continue                      # a plain text form field, not the file
        data = part.get_payload(decode=True)
        if data:
            return name, (part.get_content_type() or ""), data
    return None, None, None


def _sniff_image_media(data: bytes) -> str:
    """Detect the real image type from magic bytes → an Anthropic-accepted media_type, or ''.
    iOS hands a PWA-picked screenshot to /capture with an EMPTY or octet-stream MIME, which made
    the endpoint reject a perfectly good PNG as 'HEIC?'. The bytes never lie — trust them."""
    if not data:
        return ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _capture_kind(ctype: str, filename: str, data: bytes = b"") -> str:
    """image | pdf | audio | '' — falls back to the extension, then to magic bytes, because iOS
    loves octet-stream and empty MIME types on PWA uploads."""
    ctype = (ctype or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype == "application/pdf":
        return "pdf"
    if ctype.startswith("audio/") or ctype.startswith("video/"):
        return "audio"
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in (filename or "") else ""
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"):
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in (".m4a", ".mp3", ".wav", ".webm", ".ogg", ".aac", ".mp4", ".caf"):
        return "audio"
    if _sniff_image_media(data):          # last resort: the bytes are an image even if nothing said so
        return "image"
    if data[:5] == b"%PDF-":
        return "pdf"
    return ""


def _capture_json(raw: str) -> dict:
    """Model replies are asked for strict JSON; take the outermost object anyway so one
    stray sentence of preamble can't throw away a whole capture."""
    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        out = json.loads(s[i:j + 1])
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


@app.post("/capture", dependencies=[Depends(require_auth)])
async def capture(request: Request, dry_run: bool = False):
    """CAPTURE ANYTHING — multipart file in, filed knowledge out.

    IMAGE → Claude vision. PDF → Claude document block. AUDIO → Scribe, then the same
    extraction over the transcript. Facts land in memory (reconciled), to-dos land on the
    board (deduped server-side), contacts fold into facts. Returns the summary immediately
    so the UI can show what it got instead of a spinner and a prayer.

    dry_run=true → PREVIEW: read and return exactly what would be filed, but write NOTHING
    to memory or the board. Lets Brady eyeball a dense screenshot's read before committing.
    """
    from .brain import add_memory

    body = await request.body()
    if len(body) > CAPTURE_MAX_BYTES + (1 << 20):    # +1MB slack for the multipart envelope
        return {"ok": False, "error": "That file is too big (over 18MB). Send a photo or a shorter clip."}
    try:
        filename, ctype, data = _capture_part(body, request.headers.get("content-type", ""))
    except Exception as e:
        logger.warning("capture: multipart parse failed: %s", e)
        return {"ok": False, "error": "Couldn't read that upload. Try again."}
    if not data:
        return {"ok": False, "error": "No file came through."}
    if len(data) > CAPTURE_MAX_BYTES:
        return {"ok": False, "error": "That file is too big (over 18MB). Send a photo or a shorter clip."}

    kind = _capture_kind(ctype, filename or "", data)
    if not kind:
        return {"ok": False, "error": "I can read images, PDFs and audio. That one I can't."}

    transcript = ""
    b64 = ""
    if kind == "image":
        media = (ctype or "").lower()
        if media not in CAPTURE_IMAGE_TYPES:
            media = _sniff_image_media(data)   # iOS mislabels/omits the MIME — trust the bytes
        if media not in CAPTURE_IMAGE_TYPES:
            return {"ok": False, "error": "That image format won't open (HEIC?). Send it as a JPEG or PNG."}
        if len(data) > CAPTURE_IMAGE_MAX_BYTES:
            return {"ok": False, "error": "That photo is too large to read (over 5MB). Shrink it and resend."}
        b64 = base64.standard_b64encode(data).decode()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
            {"type": "text", "text": _CAPTURE_ASK},
        ]
    elif kind == "pdf":
        b64 = base64.standard_b64encode(data).decode()
        content = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": _CAPTURE_ASK},
        ]
    else:
        transcript, err = await voice.transcribe(data, filename or "memo.m4a", ctype or "audio/m4a")
        if not transcript:
            return {"ok": False, "error": "Couldn't hear that one" + (f" ({err})" if err else "") + "."}
        content = [{"type": "text",
                    "text": _CAPTURE_ASK + "\n\nVOICE MEMO TRANSCRIPT:\n" + transcript}]

    try:
        client = chat._anthropic()
        resp = await client.messages.create(
            model=chat.LEARN_MODEL, max_tokens=3000,
            messages=[{"role": "user", "content": content}])
        parsed = _capture_json("".join(getattr(b, "text", "") for b in resp.content))
    except Exception as e:
        logger.error("capture: extraction failed (%s): %s", kind, e)
        out = {"ok": False, "error": "I couldn't read that one. Try again in a second."}
        if dry_run:   # surface the real cause through the safe preview path for debugging
            out["detail"] = (type(e).__name__ + ": " + str(e))[:400]
        return out

    # Contacts are facts with a shape — fold them in rather than inventing a second store.
    facts = [f.strip() for f in (parsed.get("facts") or []) if isinstance(f, str) and f.strip()]
    for c in (parsed.get("contacts") or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        reach = ", ".join(x for x in (str(c.get("phone") or "").strip(),
                                      str(c.get("email") or "").strip()) if x)
        note = str(c.get("note") or "").strip()
        facts.append("Contact — " + name + (": " + reach if reach else "")
                     + (" — " + note if note else ""))

    # PREVIEW MODE — read it, show what it found, file nothing. Return the exact facts/todos
    # so Brady can confirm a dense screenshot read right before it touches his brain.
    if dry_run:
        prev_todos = []
        for t in (parsed.get("todos") or []):
            t = {"text": t} if isinstance(t, str) else t
            if isinstance(t, dict) and len(str(t.get("text") or "").strip()) >= 4:
                prev_todos.append(str(t["text"]).strip())
        return {
            "ok": True, "dry_run": True, "kind": kind,
            "summary": str(parsed.get("summary") or "").strip(),
            "facts": facts, "todos": prev_todos,
            "facts_count": len(facts), "todos_count": len(prev_todos),
            "text": transcript or str(parsed.get("text") or "").strip(),
        }

    filed_todos = []
    for t in (parsed.get("todos") or []):
        if isinstance(t, str):
            t = {"text": t}
        if not isinstance(t, dict):
            continue
        txt = str(t.get("text") or "").strip()
        if len(txt) < 4:
            continue
        cat = str(t.get("category") or "").strip()
        cat = cat if cat in CAPTURE_CATEGORIES else "Admin"
        ok, res = await asyncio.to_thread(daybank.add_item, "todo", txt, None, [cat, "capture"])
        if ok and not (isinstance(res, dict) and res.get("dup")):
            filed_todos.append(txt)

    if facts:
        await asyncio.to_thread(add_memory, facts, "capture")

    summary = str(parsed.get("summary") or "").strip()
    raw_text = transcript or str(parsed.get("text") or "").strip()
    if not summary:
        summary = "Read it — nothing in there worth filing."
    line = "📎 Capture (" + kind + ") — " + summary
    if facts or filed_todos:
        line += ("\n\nFiled: " + str(len(facts)) + " fact(s) to memory, "
                 + str(len(filed_todos)) + " to-do(s) to the board.")
    await asyncio.to_thread(history.append, "assistant", line)

    return {
        "ok": True,
        "kind": kind,
        "summary": line,
        "facts_count": len(facts),
        "todos_count": len(filed_todos),
        "text": raw_text,
    }


# ── Full-duplex realtime voice (ElevenLabs Agents) ──────────────────────────────
@app.get("/diag", dependencies=[Depends(require_auth)])
async def diag():
    """Operator check-up: the live VOICE context cache (what a call actually reads) + the
    ElevenLabs agent's dashboard prompt (where a stale hardcoded date could hide). Read-only."""
    return {
        "context_cache": chat.ctx_diag(),
        "elevenlabs_agent": await voice.convai_agent_audit(),
    }


@app.get("/diag/mcp", dependencies=[Depends(require_auth)])
async def diag_mcp():
    """What Google Workspace (MCP) tools Ace's TYPED loop currently has loaded — the create/
    write surface that voice never carries. Read-only; empty/count 0 = connection dormant."""
    from .integrations import mcp_client
    schemas = await mcp_client.tool_schemas()
    return {"enabled": mcp_client.enabled(),
            "url_set": bool(mcp_client._url()),
            "sdk": mcp_client._SDK,
            "sdk_error": getattr(mcp_client, "_SDK_ERR", ""),
            "sdk_names": getattr(mcp_client, "_SDK_NAMES", ""),
            "count": len(schemas),
            "tools": sorted(s["name"] for s in schemas)}


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


# ── PHONE PUSH — Ace reaches Brady when the app is CLOSED ───────────────────────
# The gap this closes: proactive briefs only ever landed if a HUD tab was open
# (publish_stage_event talks to live WebSockets). Web push reaches the installed PWA
# with nothing running.
#
# DORMANT BY DEFAULT, exactly like db.py: everything is gated on VAPID_PUBLIC_KEY +
# VAPID_PRIVATE_KEY. Unset → /push/key returns null, the HUD never offers the prompt,
# and send_push() logs ONE warning and returns. A brief must never fail — or even
# slow down — because phone alerts aren't configured yet.
_push_warned = [False]     # one warning per process, not one per brief


def _vapid() -> tuple:
    """(public, private, subject). Subject is the RFC-8292 contact the push services
    require — a real mailto:/https: they can reach if a sender misbehaves."""
    return (
        os.environ.get("VAPID_PUBLIC_KEY", "").strip(),
        os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
        os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:pfi@platinumfortuneimpact.com",
    )


def push_configured() -> bool:
    pub, priv, _ = _vapid()
    return bool(pub and priv)


def _push_warn(msg: str, *args) -> list:
    if not _push_warned[0]:
        _push_warned[0] = True
        logger.warning("push: " + msg + " — phone alerts dormant", *args)
    return []


def _push_now(title: str, body: str, url: str) -> list:
    """BLOCKING fan-out to every stored device; returns per-endpoint results. Run it in a
    thread (send_push does) — pywebpush is sync `requests` under the hood. A device that
    answers 404/410 is gone for good (app deleted, endpoint rotated), so it's DROPPED
    rather than retried forever."""
    pub, priv, subject = _vapid()
    if not (pub and priv):
        return _push_warn("VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY unset")
    try:
        from pywebpush import WebPushException, webpush
    except Exception as e:
        return _push_warn("pywebpush not installed (%s)", e)
    if not db.enabled():
        return _push_warn("no DATABASE_URL, nowhere to store subscriptions")
    subs = db.list_push_subs()
    payload = json.dumps({"title": title, "body": body, "url": url or "/"})
    out = []
    for s in subs:
        tail = s["endpoint"][-24:]
        try:
            # Fresh claims dict PER endpoint: pywebpush mutates it in place to stamp in
            # the push service's `aud`, so a shared dict would sign Google's push with
            # Apple's audience on the second device and earn a 403.
            webpush(subscription_info=s, data=payload, vapid_private_key=priv,
                    vapid_claims={"sub": subject}, ttl=3600, timeout=10)
            out.append({"endpoint": tail, "ok": True})
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code in (404, 410):
                db.remove_push_sub(s["endpoint"])
            out.append({"endpoint": tail, "ok": False, "status": code, "error": str(e)[:160]})
        except Exception as e:
            out.append({"endpoint": tail, "ok": False, "error": str(e)[:160]})
    if out:
        logger.info("push sent: %d ok / %d devices", sum(1 for r in out if r["ok"]), len(out))
    return out


def send_push(title: str, body: str, url: str = "/") -> None:
    """FIRE-AND-FORGET phone alert. Safe from anywhere — sync or async, hot path or not:
    it hands off to a daemon thread and returns immediately, and it never raises."""
    try:
        threading.Thread(target=_push_now, name="ace2-push", daemon=True,
                         args=(title or "ACE", (body or "").strip()[:180], url or "/")).start()
    except Exception as e:
        logger.warning("push: send_push could not start a thread: %s", e)


class PushSubReq(BaseModel):
    endpoint: str = ""
    keys: dict = {}


class PushUnsubReq(BaseModel):
    endpoint: str = ""


@app.get("/push/key", dependencies=[Depends(require_auth)])
async def push_key():
    """The VAPID public key the browser needs to subscribe. null = not configured yet,
    which is the HUD's signal to never offer phone alerts at all."""
    pub, priv, _ = _vapid()
    return {"publicKey": pub if (pub and priv) else None, "storage": db.enabled()}


@app.post("/push/subscribe", dependencies=[Depends(require_auth)])
async def push_subscribe(req: PushSubReq):
    """Store the device's PushSubscription (posted verbatim from pushManager.subscribe)."""
    if not db.enabled():
        return {"ok": False, "error": "no DATABASE_URL — nowhere to store the subscription"}
    ok = await asyncio.to_thread(db.add_push_sub, {"endpoint": req.endpoint, "keys": req.keys})
    if ok:
        logger.info("push: device subscribed (…%s)", req.endpoint[-24:])
    return {"ok": ok}


@app.post("/push/unsubscribe", dependencies=[Depends(require_auth)])
async def push_unsubscribe(req: PushUnsubReq):
    return {"ok": await asyncio.to_thread(db.remove_push_sub, req.endpoint)}


@app.post("/push/test", dependencies=[Depends(require_auth)])
async def push_test():
    """Send a test notification to every stored device and report what each one said —
    the single call that proves the whole chain: keys → subscription → Apple/Google → SW."""
    if not push_configured():
        return {"ok": False, "error": "VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY not set", "results": []}
    results = await asyncio.to_thread(_push_now, "ACE", "Phone alerts are live. ◈", "/")
    return {"ok": any(r.get("ok") for r in results), "devices": len(results), "results": results}


# ── MAX BRIDGE — Brady's iMac runs Ace's heavy background thinking on his Claude ──
# Max plan ($0/token) instead of the metered API. The server stays the ONE brain:
# it composes the exact prompts its own loops use and applies results through the
# exact same guarded stores (reconcile-on-write memory, dedup'd board, claim-first
# briefs) — the worker just supplies the electricity. Mac asleep? The in-server
# loops run on the API exactly as before. NO function ever depends on the bridge.
# Auth: header X-Bridge-Key must equal env ACE2_BRIDGE_KEY (unset → bridge dormant).
def _bridge_check(request: Request):
    key = os.environ.get("ACE2_BRIDGE_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="bridge disabled — set ACE2_BRIDGE_KEY")
    if request.headers.get("x-bridge-key", "") != key:
        raise HTTPException(status_code=401, detail="bad bridge key")


@app.get("/bridge/jobs")
async def bridge_jobs(request: Request):
    """Due background jobs with ready-to-run prompts. Briefs are CLAIMED here
    (claim-first, same rule as the loop) so bridge + loop can never double-send."""
    _bridge_check(request)
    from . import db as _db
    jobs = []
    now = chat.datetime.now(chat.EASTERN)
    today = now.strftime("%Y-%m-%d")
    cur = now.hour * 60 + now.minute
    for kind, (hh, mm) in chat._BRIEF_TIMES.items():
        if not (0 <= cur - (hh * 60 + mm) <= 120):
            continue   # only within the 2h window — same rule as _brief_loop
        if chat._brief_sent.get(kind) == today:
            continue
        last = await asyncio.to_thread(_db.latest_summary, f"brief_{kind}")
        if (last.get("text") or "") == today:
            chat._brief_sent[kind] = today
            continue
        # CLAIM FIRST — BEFORE the multi-second compose (2026-08-11 review fix). _brief_sent is
        # shared in-process with _brief_loop, so claiming now makes the loop skip; claiming AFTER
        # compose left a window where the loop could send the same brief (double push).
        chat._brief_sent[kind] = today
        await asyncio.to_thread(_db.add_summary, today, f"brief_{kind}")
        prompt = await chat.compose_brief_prompt(kind)
        if not prompt:
            continue   # claimed but nothing to compose (rare); never double-sends
        jobs.append({"job": "brief", "kind": kind, "prompt": prompt, "max_tokens": 400})
    sweep = await chat.compose_sweep()
    if not sweep.get("skipped"):
        # Claim the sweep hash so the in-server _learn_loop won't ALSO run it on the metered API
        # (the double-burn the Max bridge exists to prevent).
        chat._learn_state["last_hash"] = sweep["hash"]
        jobs.append({"job": "sweep", "hash": sweep["hash"],
                     "facts_prompt": sweep["facts_prompt"],
                     "triage_prompt": sweep["triage_prompt"],
                     "reflection_prompt": sweep["reflection_prompt"]})
    return {"jobs": jobs, "server_time": now.isoformat()}


class BridgeResultReq(BaseModel):
    job: str = ""          # "brief" | "sweep"
    kind: str = ""         # brief: morning | eod
    text: str = ""         # brief body
    hash: int = 0          # sweep conversation hash (stamps last_hash on apply)
    facts: str = ""
    triage: str = ""
    reflection: str = ""


@app.post("/bridge/complete")
async def bridge_complete(req: BridgeResultReq, request: Request):
    """Apply one finished job through the same guarded delivery/filing paths."""
    _bridge_check(request)
    if req.job == "brief" and req.kind in ("morning", "eod"):
        delivered = await chat.deliver_brief(req.kind, req.text)
        return {"ok": bool(delivered), "kind": req.kind}
    if req.job == "sweep":
        res = await chat.apply_sweep(req.hash, req.facts, req.triage, req.reflection)
        return {"ok": "error" not in res, **res}
    return {"ok": False, "error": "unknown job"}


# ── Static frontend (mounted last so API routes win) ────────────────────────────
@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
