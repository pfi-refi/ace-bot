"""
Ace 2.0 chat turn — streaming, with real tool use.

A manual streaming loop over the Anthropic Messages API (not the Tool Runner):
the runner is great for headless agents, but here the harness IS the product —
we emit a custom WebSocket event the instant each tool runs, so the HUD orb can
show "◈ CREATING EVENT…" between bursts of text. That interleaving is exactly
the control a manual loop gives cleanly.

WS event protocol (matches app.js):
    start        — a turn began
    delta {text} — a chunk of Ace's reply
    tool  {name,label,status} — a tool is running / finished (JARVIS moment)
    confirmation {text}       — a tool's result line (also pushed to intel feed)
    final {text} — the complete reply text
    error {text}
    done

Design decisions carried in from the portal's scars:
  • Live context is fetched CONCURRENTLY (asyncio.gather over to_thread), never
    six sequential blocking calls. Fatal for realtime voice otherwise.
  • Conversation history is READ from the shared Drive file for continuity and
    sanitized before it touches the API; 2.0 never writes that file (brain.py).
  • Actions execute mid-turn, BEFORE the final text — so the confirmation is true
    by the time Ace says it (the portal ran tags after emitting the reply).
  • No tags anywhere. The prompt describes tools; there's nothing to regress into.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

import pytz
from anthropic import AsyncAnthropic

from . import brain, daybank, history, tools
from .integrations.calendar_api import (
    get_events_structured,
    get_tomorrow_events,
)
from .integrations import mcp_client
from .integrations.tasks_api import (
    add_task, get_gmail_summary, get_inbox_structured, get_task_lists_grouped, get_tasks,
    get_tasks_structured,
)
from .integrations.weather import get_weather
from .system_prompt import build_system_prompt

logger = logging.getLogger("ace2.chat")

EASTERN = pytz.timezone("America/New_York")
# LEAN MODE (2026-08-03, money tight): typed brain on Sonnet 5 — intro pricing $2/$10
# through 2026-08-31 (vs Opus $5/$25 = 60%+ cheaper), near-Opus quality for chief-of-staff
# work. Revert when cash flows: set ACE2_MODEL=claude-opus-4-8 in Railway or flip this default.
MODEL = os.environ.get("ACE2_MODEL", "claude-sonnet-5")
# Live VOICE replies run on the FASTEST model, not Opus — conversational snappiness
# (time-to-first-word) matters far more than depth per spoken sentence, and Opus's
# thinking latency is the main thing that makes voice feel laggy. Typed stays on MODEL.
# Bump to claude-sonnet-5 via env if voice needs more reasoning per turn.
VOICE_MODEL = os.environ.get("ACE2_VOICE_MODEL", "claude-haiku-4-5-20251001")
# The background LEARNING/TRIAGE sweep + briefs + graph + nudges are NOT latency-bound (they
# run off the live path), so they don't need the premium typed brain (Opus, $5/$25). Sonnet 5
# ($3/$15, cheaper still on intro pricing) is near-Opus quality and cut Brady's API bill ~40-60%
# on all this invisible background work — the typed CHAT he actually interacts with stays MODEL.
# Haiku was too weak here (a dense brain dump → 0 facts, most dropped); Sonnet 5 is the floor.
# Override with ACE2_LEARN_MODEL (e.g. back to claude-opus-4-8) to trade cost for depth.
# LEAN MODE: ALL background work (sweep, briefs, watchdog, recap, graph) rides this one
# knob — Haiku 4.5 ($1/$5) does structured triage/recap work fine. Was Sonnet 5.
LEARN_MODEL = os.environ.get("ACE2_LEARN_MODEL", "claude-haiku-4-5-20251001")
# On voice Ace gets the FULL toolset — send_email included as of 2026-07-19, because the
# confirm-before-execute gate below now guards it on every path (Ace asks out loud, Brady
# says yes, only then does the second call actually send). Built once for cache stability.
_VOICE_TOOL_DENY = set()

# ---- Confirm-before-execute gate (guardrails, 2026-07-19) ----------------------------
# Two-phase confirm for outward/destructive tools. A gated tool NEVER executes on its
# first call: it arms a per-TOOL "permission ticket" and returns CONFIRMATION REQUIRED,
# so Ace must state exactly what he's about to do and ask Brady. It executes only when
# (a) the model re-calls with "confirmed": true, (b) a ticket for that tool was armed on
# an EARLIER turn (so Ace cannot self-approve inside one turn), and (c) the ticket is
# still fresh. `_turn_seq` is a monotonic counter bumped once per stream_turn call —
# immune to the 160-message transcript cap that a message-count deadlocks on. On the
# yes-turn we execute the args the model RE-SENDS (no byte-identical replay), so a fresh
# email body never livelocks the confirm.
_CONFIRM_ALWAYS = {
    "send_email", "mcp_send_gmail_message",   # outbound to third parties
    "delete_calendar_event",                  # destroys a meeting
    "mcp_get_drive_shareable_link",           # outward data exposure
}
_DESTRUCTIVE_ACTIONS = {"delete", "remove", "clear", "cancel", "trash"}
_DESTRUCTIVE_HINTS = ("delete", "remove", "clear", "trash", "cancel")
_SHARE_UPDATES = {"all", "externalonly", "true", "1"}
_CONFIRM_TTL_SEC = 300
_pending_confirm: dict = {}   # tool_name -> {"turn": int, "ts": float}
_turn_seq = [0]

_CONFIRM_MSG = (
    "CONFIRMATION REQUIRED — nothing was executed. Tell Brady in ONE plain sentence exactly "
    "what this will do (for an email: who it goes to and the subject; for a delete or change: "
    "which item and what happens to it) and ask him to confirm. Do NOT say it's done — it has "
    "not happened. Only after he clearly says yes on a LATER turn, call this same tool again "
    "with the same details plus \"confirmed\": true. If he wants any change, call it again with "
    "the updated details (no confirmed) so he can okay the new version. If he says no, drop it."
)
# Second+ block of the SAME tool inside one turn: the model is churning (re-calling with
# confirmed:true in the same breath — approval can only arrive on Brady's NEXT turn). Cut the
# retry loop dead: a hard STOP that leaves it exactly one legal move.
_CONFIRM_STOP_MSG = (
    "STOP — do NOT call this tool (or any tool) again this turn; it will keep coming back "
    "blocked, because approval can only arrive on Brady's NEXT turn. Right now say ONE short "
    "sentence telling him exactly what you're about to do and asking him to confirm — then "
    "end your turn and wait for his answer."
)


def _next_turn_id() -> int:
    _turn_seq[0] += 1
    return _turn_seq[0]


def _strip_confirm(args: dict) -> dict:
    return {k: v for k, v in args.items() if k not in ("confirmed", "confirm_token")}


def _needs_confirm(name: str, args: dict) -> bool:
    if name in _CONFIRM_ALWAYS:
        return True
    a = _strip_confirm(args)
    if name in ("mcp_manage_event", "mcp_manage_task"):
        action = str(a.get("action", "")).strip().lower()
        if action:
            # Trust the explicit action verb — 'create'/'update' a task named
            # "Cancel Comcast" must NOT trip a destructive hint.
            if action in _DESTRUCTIVE_ACTIONS:
                return True
            return str(a.get("send_updates", "")).strip().lower() in _SHARE_UPDATES
        # No action given → sniff the whole payload as a fallback.
        blob = json.dumps(a, default=str).lower()
        return any(h in blob for h in _DESTRUCTIVE_HINTS)
    if name == "mcp_modify_sheet_values":
        return bool(a.get("clear_values"))   # clearing a range destroys data
    if name == "mcp_modify_doc_text":
        return True   # overwrites document content
    return False


def _confirm_gate(name: str, args: dict, turn_id: int):
    """Returns (blocked: bool, clean_args: dict). Arms/consumes per-tool tickets.
    clean_args always has confirm flags stripped so they never reach an executor."""
    clean = _strip_confirm(args)
    if not _needs_confirm(name, args):
        return False, clean
    now = time.time()
    for k in [k for k, v in _pending_confirm.items() if now - v["ts"] > _CONFIRM_TTL_SEC]:
        _pending_confirm.pop(k, None)
    ticket = _pending_confirm.get(name)
    if args.get("confirmed") and ticket and ticket["turn"] < turn_id:
        _pending_confirm.pop(name, None)   # single-use — a fresh call must re-arm
        return False, clean
    _pending_confirm[name] = {"turn": turn_id, "ts": now}
    return True, clean
EFFORT = os.environ.get("ACE2_EFFORT", "low")   # low|medium|high|xhigh|max — LEAN MODE: was medium
MAX_TOKENS = int(os.environ.get("ACE2_MAX_TOKENS", "16000"))  # ceiling covers thinking+tools+prose; only billed if used
MAX_TOOL_ITERS = 8
NOW_WINDOW_MIN = 90  # an event that started within this many minutes reads as "in progress"

_client = None


def _format_today_schedule(events: list, now: datetime) -> str:
    """Split today's structured events into done / now / next / later for the model.

    `events` are get_events_structured() dicts (each has an Eastern-aware `iso` and a
    formatted `time`). Rendering the day relative to `now` is what lets Ace reason in
    "what's already happened vs. what's next" terms instead of seeing a flat list.
    """
    timed, all_day = [], []
    for e in events:
        if e.get("all_day"):
            all_day.append(e)
            continue
        try:
            start = datetime.fromisoformat(e["iso"])
        except (ValueError, KeyError, TypeError):
            continue
        timed.append((start, e))
    timed.sort(key=lambda x: x[0])

    past = [(s, e) for s, e in timed if s <= now]
    upcoming = [(s, e) for s, e in timed if s > now]

    # The most-recent already-started event counts as "in progress" if it began
    # within the window (structured events carry no end time, so this approximates).
    now_item, done = None, list(past)
    if past and (now - past[-1][0]) <= timedelta(minutes=NOW_WINDOW_MIN):
        now_item, done = past[-1], past[:-1]

    lines = []
    if done:
        lines.append("Already done earlier today:")
        lines += [f"  ✓ {e['time']} — {e['title']}" for _, e in done]
    if now_item:
        lines.append("Happening now (started recently):")
        lines.append(f"  ▸ {now_item[1]['time']} — {now_item[1]['title']}")
    if upcoming:
        lines.append("NEXT UP:")
        lines.append(f"  → {upcoming[0][1]['time']} — {upcoming[0][1]['title']}")
        if len(upcoming) > 1:
            lines.append("Later today:")
            lines += [f"  • {e['time']} — {e['title']}" for _, e in upcoming[1:]]
    if all_day:
        lines.append("All day:")
        lines += [f"  • {e['title']}" for e in all_day]

    return "\n".join(lines) if lines else "(nothing on the calendar today)"


def _anthropic() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


async def _live_context() -> str:
    """Fetch memory + calendar (recent past → next 3 weeks) + tasks + inbox + weather
    + data bank concurrently."""
    memory, cal_all, bank, inbox, wx = await asyncio.gather(
        asyncio.to_thread(brain.read_memory),
        asyncio.to_thread(get_events_structured, 21, 7),  # last week → next 3 weeks
        asyncio.to_thread(daybank.read_items, True),
        asyncio.to_thread(get_gmail_summary),
        get_weather(),
        return_exceptions=True,
    )

    def ok(v, default):
        return default if isinstance(v, Exception) else v

    now = datetime.now(EASTERN)
    events = ok(cal_all, [])
    today_str = now.strftime("%Y-%m-%d")
    today_events = [e for e in events if e.get("date") == today_str]
    mem_list = ok(memory, [])
    mem = "\n".join(f"- {m}" for m in mem_list) if mem_list else "(memory empty)"
    today_sched = _format_today_schedule(today_events, now)
    bank_str = _format_daybank(ok(bank, []))
    parts = [
        f"CURRENT TIME (Eastern): {now.strftime('%A, %B %d, %Y — %-I:%M %p')}",
        "",
        "ACE MEMORY (what you know about Brady and PFI):",
        mem,
        "",
        "WHERE YOU LEFT OFF (recap of your recent conversations — pick up from here, don't re-ask):",
        _recap_block(),
        "",
        "TODAY'S SCHEDULE (relative to the current time above):",
        today_sched,
        "",
        "CALENDAR — last week through the next 3 weeks (past for reference, upcoming for "
        "planning; answer any date-range question from this directly):",
        _format_calendar_window(events, now),
        "",
        "UNREAD PRIORITY INBOX (last 2 days — scan it; flag anything that needs a reply):",
        ok(inbox, "(unavailable)"),
        "",
        "WEATHER RIGHT NOW (factor it into his day when it matters):",
        _format_weather(ok(wx, {})),
        "",
        "YOUR TASK BOARD (Ace's OWN store — THE task & pipeline system; Brady sees this as his "
        "Command panel, organized by category. Each item has an id: complete/reopen with "
        "update_item, capture new ones with capture_item. Google Tasks is retired — never route "
        "tasks there unless Brady explicitly says 'Google'):",
        bank_str,
    ]
    return "\n".join(parts)


def _format_weather(w) -> str:
    if not isinstance(w, dict) or not w.get("ok"):
        return "(unavailable)"
    lead = []
    if w.get("temp") is not None:
        lead.append(f"{w['temp']}°")
    if w.get("description"):
        lead.append(w["description"])
    hl = []
    if w.get("high") is not None:
        hl.append(f"H{w['high']}°")
    if w.get("low") is not None:
        hl.append(f"L{w['low']}°")
    tail = f" ({'/'.join(hl)})" if hl else ""
    loc = f" — {w['location']}" if w.get("location") else ""
    return (" ".join(lead) + tail + loc) or "(unavailable)"


def _format_daybank(items: list) -> str:
    """Render the data bank for Ace's context: open items first, then today's done."""
    if not items:
        return "(nothing captured yet)"
    open_items = [it for it in items if it.get("status") == "open"]
    done_today = [it for it in items if it.get("status") == "done"]
    _CATS = {"Deals", "Agents", "Admin", "Networking", "Business", "Tech", "Personal", "Goals"}

    def _cat(it):
        for t in (it.get("tags") or []):
            if t in _CATS:
                return f" [{t}]"
        return ""

    lines = []
    for it in open_items:
        due = f" (due {it['due']})" if it.get("due") else ""
        lines.append(f"- [{it.get('id','?')}]{_cat(it)} {it.get('kind','note')}: {it.get('text','')}{due}")
    for it in done_today:
        lines.append(f"- [{it.get('id','?')}] ✓ done: {it.get('text','')}")
    return "\n".join(lines)


async def _load_messages(user_text: str, prior=None) -> list:
    """Conversation for the API, sanitized to {role,content} + this turn.

    prior=None (HTTP one-shot): seed from the UNIFIED thread (ace2's own history,
    which records both voice and typed turns). prior given (WS chat / voice adapter):
    use the caller's conversation — the WS seeds it from the unified thread too, and
    the voice adapter passes ElevenLabs' live call history.
    """
    if prior is None:
        prior = await asyncio.to_thread(_unified_thread)
    msgs = brain.sanitize_for_api(prior)
    if not msgs or msgs[-1].get("content") != user_text:
        msgs.append({"role": "user", "content": user_text})
    return msgs


# ── The ONE brain: Ace 2.0 is SELF-CONTAINED ────────────────────────────────────
# Ace records EVERY turn from both voice and chat to its OWN history. That log — not
# the Telegram bot's shared window — is the single source of truth. Ace no longer
# reads the bot's conversation at all: the bot is a standalone backup now, never a
# dependency. This is what makes voice and chat the same brain AND makes Ace fully
# individual — the bot going dark can't take Ace's memory with it. Durable state
# lives in ACE MEMORY (facts) + the DATA BANK (commitments/deals); this raw thread
# is only the last handful of turns for immediate continuity.
def _unified_thread(limit: int = 30) -> list:
    """Recent conversation across voice + chat, from Ace's OWN history only. Widened 12→30
    (Brady: "I always want him to remember — that's building context"); recall covers anything
    older than this window."""
    try:
        entries = history.read_recent(2)
    except Exception:
        entries = []
    turns = [
        {"role": e.get("role"), "content": (e.get("content") or "").strip(), "ts": e.get("ts")}
        for e in entries
        if e.get("role") in ("user", "assistant") and (e.get("content") or "").strip()
    ]
    return turns[-limit:]


def _format_thread(turns: list) -> str:
    """Render the recent thread for the context block. Lines are stamped with the DAY
    ONLY (never a clock time) — day-stamps stop the 'it's Saturday' drift, while
    omitting the time stops Ace from echoing a past turn's timestamp as 'now' (the
    9:24-vs-10:12 bug). The one authority for the current time is the CURRENT TIME
    line at the top of the context."""
    if not turns:
        return "(no earlier conversation yet — start fresh)"
    lines = []
    for m in turns:
        stamp = ""
        if m.get("ts"):
            try:
                dt = datetime.fromisoformat(m["ts"]).astimezone(EASTERN)
                stamp = f"[{dt.strftime('%a')}] "
            except Exception:
                stamp = ""
        lines.append(f"{stamp}{m['role']}: {m['content'][:280]}")
    return "\n".join(lines)


async def _card_payload(panel: str):
    """Structured data for a display_card call. Concurrent, graceful."""
    if panel == "calendar":
        return {"events": await asyncio.to_thread(get_events_structured, 21, 7)}
    if panel == "timeline":
        # Today only; the frontend computes the NOW line against the live clock.
        return {"events": await asyncio.to_thread(get_events_structured, 1)}
    if panel == "tasks":
        flat, grouped = await asyncio.gather(
            asyncio.to_thread(get_tasks_structured),
            asyncio.to_thread(get_task_lists_grouped),
        )
        return {"tasks": flat, "lists": grouped}
    if panel == "inbox":
        return {"emails": await asyncio.to_thread(get_inbox_structured, 6)}
    if panel == "weather":
        return await get_weather()
    if panel == "memory":
        return {"memories": await asyncio.to_thread(brain.read_memory)}
    if panel == "daybank":
        return {"items": await asyncio.to_thread(daybank.read_items, True)}
    return {}


async def _run_ui_tool(name: str, tool_input: dict, emit) -> str:
    """Execute a UI tool: the 'action' is an event the frontend materializes."""
    if name == "display_card":
        panel = (tool_input.get("panel") or "").strip().lower()
        where = (tool_input.get("where") or "").strip().lower()
        where = where if where in ("left", "right") else None
        try:
            data = await _card_payload(panel)
            await emit("card", {"panel": panel, "data": data, "where": where})
            return f"Displayed the {panel} card on screen."
        except Exception as e:
            logger.error("display_card(%s): %s", panel, e)
            return f"⚠️ Could not display {panel}: {e}"
    if name == "open_url":
        url = (tool_input.get("url") or "").strip()
        label = (tool_input.get("label") or url).strip()
        if not url.lower().startswith(("http://", "https://")):
            return "⚠️ open_url needs an http(s) URL."
        await emit("open", {"url": url, "label": label})
        return f"Opened on screen: {label}"
    return f"⚠️ Unknown UI tool: {name}"


def _format_calendar_window(events: list, now) -> str:
    """Group a get_events_structured(days, back_days) list by date for context — recent
    past + upcoming — so Ace can answer 'what did I have Tuesday' and 'what's next week'.
    """
    if not events:
        return "(no events in this window)"
    today = now.date()
    by_date: dict = {}
    for e in events:
        by_date.setdefault(e.get("date", ""), []).append(e)
    lines = []
    for dstr in sorted(k for k in by_date if k):
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
        except Exception:
            continue
        delta = (d - today).days
        rel = {0: " — TODAY", -1: " — YESTERDAY", 1: " — TOMORROW"}.get(delta, " (past)" if delta < 0 else "")
        header = by_date[dstr][0].get("date_label", dstr)
        lines.append(f"{header}{rel}:")
        for e in by_date[dstr]:
            cal = e.get("calendar", "")
            lines.append(f"  {e.get('time','')} — {e.get('title','')}" + (f" [{cal}]" if cal else ""))
    return "\n".join(lines)


# ── Voice context cache (stale-while-revalidate) ────────────────────────────────
# _fast_context() used to run a live Google-Calendar + Drive-memory + weather + Postgres
# fan-out on EVERY voice turn (tolerated up to 9s). THAT silent gap is what makes ElevenLabs
# cut a live call — the timeouts Brady keeps hitting. Voice conversation doesn't need
# second-fresh calendar/memory (ElevenLabs resends the live turn-by-turn transcript as
# `prior`), so we serve those from a background-refreshed cache and format the per-turn
# string from cache + the LIVE clock: microseconds, zero per-turn network. Primed at startup
# (main.prime) and kept warm every ~30s.
_CTX = {"memory": [], "events": [], "bank": [], "convo": [], "wx": {}, "tasks": [], "recap": "", "ts": 0.0}
_CTX_TTL = 45.0


def ctx_diag() -> dict:
    """Read-only snapshot of the VOICE context cache — the exact data a live call reads
    from. If today's events are missing here, voice is calendar-blind even though the typed
    path (a fresh fetch) is fine. This is how we tell a stale/failed refresh from a wrong
    clock."""
    now = datetime.now(EASTERN)
    today = now.strftime("%Y-%m-%d")
    events = _CTX.get("events") or []
    todays = [e for e in events if e.get("date") == today]
    return {
        "server_now_eastern": now.strftime("%A, %B %d, %Y — %-I:%M %p"),
        "today": today,
        "cache_age_seconds": round(time.time() - _CTX["ts"], 1) if _CTX["ts"] else None,
        "cache_ttl_seconds": _CTX_TTL,
        "cache_is_stale": (_CTX["ts"] == 0.0) or (time.time() - _CTX["ts"]) > _CTX_TTL,
        "events_total_cached": len(events),
        "events_today_count": len(todays),
        "events_today_titles": [f"{e.get('time','')} {e.get('title','')}".strip() for e in todays[:12]],
        "memory_facts_cached": len(_CTX.get("memory") or []),
        "board_items_cached": len(_CTX.get("bank") or []),
        "learn_model": LEARN_MODEL,
        "typed_model": MODEL,
        "voice_model": VOICE_MODEL,
    }
_RECAP_TTL = 3 * 3600.0   # regenerate the "where we left off" recap at most every ~3h
_recap_running = [False]
_ctx_lock: asyncio.Lock = asyncio.Lock()
_ctx_keepwarm_started = [False]


async def _refresh_ctx() -> None:
    """Fetch the heavy voice-context bundle once and store it. Never raises; keeps the last
    good value for any sub-fetch that errors so one dead integration can't blank the context.
    If a refresh is already in flight, WAIT for it to land (don't skip) — a cold first turn
    that skipped got an EMPTY context and answered 'confidently' from nothing."""
    if _ctx_lock.locked():
        async with _ctx_lock:   # queue behind the in-flight fetch; its result is ours
            return
    async with _ctx_lock:
        await _refresh_ctx_inner()


async def _refresh_ctx_inner() -> None:
    try:
        memory, cal_all, bank, convo, wx = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(brain.read_memory),
                asyncio.to_thread(get_events_structured, 21, 7),  # last week → next 3 weeks
                asyncio.to_thread(daybank.read_items, True),   # HIS task board — voice's task titles
                asyncio.to_thread(_unified_thread),   # the ONE thread: voice + chat
                get_weather(),
                return_exceptions=True,
            ),
            timeout=20,  # off the critical path now, so it can afford to wait out a slow call
        )

        def keep(v, prev):
            return prev if isinstance(v, Exception) or v is None else v

        _CTX.update(
            memory=keep(memory, _CTX["memory"]),
            events=keep(cal_all, _CTX["events"]),
            bank=keep(bank, _CTX["bank"]),
            convo=keep(convo, _CTX["convo"]),
            wx=keep(wx, _CTX["wx"]),
            ts=time.time(),
        )
    except Exception as e:
        logger.warning("voice ctx refresh failed: %s", e)
    # Keep the "where we left off" recap warm + regenerate if stale — background, never blocking.
    try:
        asyncio.create_task(_refresh_recap())
    except Exception:
        pass


async def prime_ctx() -> None:
    """Warm the cache at startup and start the background loops (called once from main.py):
    the context keep-warm AND the learning sweep (Ace teaching himself from conversations)."""
    await _refresh_ctx()
    if not _ctx_keepwarm_started[0]:
        _ctx_keepwarm_started[0] = True
        asyncio.create_task(_ctx_keepwarm())
        asyncio.create_task(_learn_loop())
        asyncio.create_task(_brief_loop())   # proactive: morning game plan + EOD recap
        asyncio.create_task(_watch_loop())   # ambient: he notices things and speaks up unasked
        asyncio.create_task(_graph_warm_loop())   # keep the knowledge map instant to open


# ── The LEARNING AGENT: a background sub-agent that sweeps conversations so Ace teaches ───
# himself — auto-extracting new durable facts (not only when he remembered to save one), on a
# schedule, OFF the live conversation loop so he stays fast. This is "he builds himself" (Brady).
_LEARN_INTERVAL = 90 * 60.0   # LEAN MODE: sweep ~every 90 min (was 45 — hash-gated, so no loss when quiet)
_learn_running = [False]
_learn_state = {"last_hash": None}


async def _learn_sweep_once(force: bool = False) -> dict:
    if _learn_running[0]:
        return {"skipped": "busy"}
    _learn_running[0] = True
    filed, routed = 0, 0
    try:
        from . import db
        if not db.enabled():
            return {"skipped": "no db"}
        turns = await asyncio.to_thread(db.recent_turns, 40)
        if not turns or len(turns) < 4:
            return {"skipped": "too few turns"}
        # Keep near-full turns: Brady's brain-dump turns run 1000-1600 chars and the old 400-char
        # cap silently dropped the SECOND half of every long turn (where most deals/people/status
        # updates lived) — a primary reason the sweep under-captured. Opus can hold the whole thing.
        convo = "\n".join(f"{t.get('role')}: {(t.get('content') or '')[:2000]}" for t in turns)
        h = hash(convo)
        if not force and h == _learn_state.get("last_hash"):
            return {"skipped": "no change"}   # nothing new since the last sweep — don't burn a call
        _learn_state["last_hash"] = h
        existing = await asyncio.to_thread(brain.read_memory)
        client = _anthropic()
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": (
                "You are Ace's background LEARNING sweep — Brady's second brain. Read this whole "
                "conversation and extract EVERY new durable fact worth remembering. Be THOROUGH, "
                "not conservative: he is brain-dumping and trusts you to catch all of it. Capture "
                "each of these when present:\n"
                "- a NEW person/prospect/agent and who they are (e.g. 'Vicks — 65, retired buggy "
                "operator at the port, new prospect')\n"
                "- any change in a deal's or person's STATUS (e.g. 'Corrine leaving the company "
                "after the Kiana Wiggins deal closes')\n"
                "- commitments, plans, splits, reschedules (e.g. 'Walter splitting his past deals "
                "50/50 with Brady'), and decisions\n"
                "Rules: skip pure small talk and things ALREADY covered by KNOWN FACTS. Convert "
                "relative dates to absolute. One fact per line, ~20 words, no bullets/numbering. "
                "Err toward capturing — a missed fact is worse than a slightly redundant one. "
                "If truly nothing new, reply with the single word NONE.\n\n"
                "KNOWN FACTS:\n" + ("\n".join(f"- {m}" for m in (existing or [])[:80]) or "(none)")
                + f"\n\nCONVERSATION:\n{convo}")}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        # No NEW facts is fine — but the two halves are INDEPENDENT. A bare `return` here
        # (the old bug) skipped task-triage entirely whenever nothing new was learnable, which
        # is the common case once a day's facts are already filed. Fall through to triage.
        if not text or text.upper().startswith("NONE"):
            facts = []
        else:
            facts = [ln.strip("-•* ").strip() for ln in text.split("\n")
                     if len(ln.strip()) > 8 and not ln.strip().upper().startswith("NONE")]
        if facts:
            await asyncio.to_thread(brain.add_memory, facts, "sweep")
            filed = len(facts)
            logger.info("learn sweep: filed %d candidate fact(s)", filed)

        # TRIAGE → Ace's OWN data bank. REWORKED 2026-07-31 (board-dedup review): the old pass
        # was a one-way valve — it could ONLY add, its "ALREADY TRACKED" guard was title-only
        # and truncated at 150, and its "specific, dated" instruction manufactured paraphrases
        # that slipped the dedup gate (Kara×2, PFI-Hub×3, Donna×2 on Brady's live board). Now it
        # is a RECONCILER: it sees ids + status, it can emit DONE to close what Brady said he
        # finished, and adding a reworded twin is explicitly forbidden. Same loop that piled the
        # board up now drains it.
        closed = 0
        try:
            from . import daybank
            existing = await asyncio.to_thread(daybank.read_items, False)
            open_items = [it for it in existing if it.get("status") == "open"]
            recent_closed = [it for it in existing if it.get("status") != "open"][:40]
            tracked = "\n".join(f"- [{it['id']}] {it.get('text', '')}" for it in open_items)
            tracked_closed = "\n".join(f"- [{it['id']}] {it.get('text', '')}" for it in recent_closed)
            resp2 = await client.messages.create(
                model=LEARN_MODEL, max_tokens=1200,
                messages=[{"role": "user", "content": (
                    "You are Ace's background board-keeper — Brady's safety net so nothing he says "
                    "slips, AND the janitor who closes what he finished. Read the conversation and "
                    "do BOTH:\n"
                    "1) COMPLETIONS — if Brady said he finished / handled / sent / booked / already "
                    "did something matching an OPEN item below (even worded differently), emit:\n"
                    "DONE :: <id>\n"
                    "2) NEW to-dos — extract genuinely NEW actionable to-dos or follow-ups he needs "
                    "to DO. CRITICAL: a task that matches ANY item below — even reworded, shortened, "
                    "expanded, or with a different date — is NOT new. Skip it (or emit DONE if he "
                    "finished it). Never re-add a CLOSED item unless Brady explicitly reopened it. "
                    "New info about a tracked task is NOT a new task. For real new ones emit:\n"
                    "ADD :: CATEGORY :: task title\n"
                    "(CATEGORY from: Deals, Agents, Admin, Networking, Business, Tech, Personal, "
                    "Goals.) One per line, ONLY those two formats, no other text. If nothing, "
                    "reply NONE.\n\n"
                    "OPEN ITEMS:\n" + (tracked or "(none)") + "\n\n"
                    "RECENTLY CLOSED (do not re-add):\n" + (tracked_closed or "(none)") + "\n\n"
                    f"CONVERSATION:\n{convo}")}])
            t2 = "".join(getattr(b, "text", "") for b in resp2.content).strip()
            if t2 and not t2.upper().startswith("NONE"):
                open_ids = {it["id"] for it in open_items}
                for ln in t2.split("\n"):
                    if "::" not in ln:
                        continue
                    parts = [p.strip().strip("-•*[] ") for p in ln.split("::")]
                    verb = parts[0].upper()
                    if verb == "DONE" and len(parts) >= 2:
                        iid = parts[1].split()[0].strip("[]") if parts[1] else ""
                        if iid in open_ids:   # only close ids that are really open — never guess
                            ok2, _r = await asyncio.to_thread(daybank.update_item, iid, "done")
                            if ok2:
                                closed += 1
                        continue
                    if verb == "ADD" and len(parts) >= 3:
                        cat, title = parts[1], "::".join(parts[2:]).strip()
                    elif verb not in ("DONE", "ADD") and len(parts) >= 2:
                        # legacy 'CATEGORY :: title' lines still land safely
                        cat, title = parts[0], "::".join(parts[1:]).strip()
                    else:
                        continue
                    if len(title) > 4:
                        ok2, res2 = await asyncio.to_thread(
                            daybank.add_item, "todo", title, None, [cat] if cat else None)
                        if ok2 and not (isinstance(res2, dict) and res2.get("dup")):
                            routed += 1
                if routed or closed:
                    logger.info("board-keeper sweep: +%d to-do(s), closed %d", routed, closed)
        except Exception as e:
            logger.warning("triage sweep (data bank) failed: %s", e)

        # REFLECTION (third pass) — the self-building seed: Ace notices where he fell short
        # or where Brady wants more, and files it as a SELF-NOTE fact. These become the
        # backlog for upgrading Ace himself (reviewed in chats/briefs, built out in dev).
        try:
            resp3 = await client.messages.create(
                model=LEARN_MODEL, max_tokens=300,
                messages=[{"role": "user", "content": (
                    "You are Ace reflecting on how to serve Brady better. From this conversation, "
                    "list up to 3 SELF-IMPROVEMENT notes — ONLY genuine signals like: something "
                    "Brady asked for that Ace couldn't do or got wrong, a capability he wished "
                    "for, repeated manual work Ace should automate, or a misroute/miss. ~15 words "
                    "each. One per line, EXACTLY: NOTE :: <the note>. No other text. If none, "
                    "reply NONE.\n\n"
                    f"CONVERSATION:\n{convo}")}])
            t3 = "".join(getattr(b, "text", "") for b in resp3.content).strip()
            if t3 and not t3.upper().startswith("NONE"):
                notes = []
                for ln in t3.split("\n"):
                    if "::" not in ln:
                        continue   # line protocol keeps preamble/junk out of permanent memory
                    body = ln.split("::", 1)[1].strip()
                    if len(body) > 12:
                        notes.append(f"ACE SELF-NOTE: {body}")
                notes = notes[:3]
                if notes:
                    await asyncio.to_thread(brain.add_memory, notes, "reflection")
                    logger.info("reflection: filed %d self-note(s)", len(notes))
        except Exception as e:
            logger.warning("reflection pass failed: %s", e)
        return {"facts": filed, "tasks": routed, "closed": closed}
    except Exception as e:
        logger.warning("learn sweep failed: %s", e)
        return {"error": str(e)}
    finally:
        _learn_running[0] = False


async def _learn_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_LEARN_INTERVAL)
            await _learn_sweep_once()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _graph_warm_loop() -> None:
    """Keep the knowledge graph's cache hot. Building it costs ~35s of model time, and the
    HUD holds a connection open while it runs — so a cold tap on ◉ Graph is the one place
    Ace feels slow. Rebuilding just under the 6h TTL means Brady always gets the cached
    read (~0.1s) and never waits for a build he didn't ask for."""
    await asyncio.sleep(90)   # let the app finish booting before spending model time
    while True:
        try:
            from . import db
            if db.enabled():
                from .main import graph
                await graph(refresh=1)
                logger.info("graph cache warmed")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("graph warm failed: %s", e)
        try:
            await asyncio.sleep(23 * 3600)   # once a day — the book of business doesn't turn
        except asyncio.CancelledError:       # over every 5h; cut this rebuild cost ~80%. TTL
            break                            # is raised to match so taps still hit the cache.


async def _ctx_keepwarm() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            await _refresh_ctx()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ── PROACTIVE BRIEFS — Ace comes to Brady (morning game plan + EOD recap) ────────
# The autonomy loop Brady asked for: Ace runs himself on a schedule. Each brief is
# grounded in DETERMINISTIC board/win stats first, then written by the deep model,
# and lands in the ONE thread (so it's waiting in chat) + pushes live to open HUDs.
_BRIEF_TIMES = {"morning": (9, 0), "eod": (20, 15)}   # Eastern — Brady wants the day to open at 9


def _board_stats() -> str:
    """Deterministic business snapshot from Ace's own store — no LLM guessing."""
    from . import db
    _CATS = ["Deals", "Agents", "Admin", "Networking", "Business", "Tech", "Personal", "Goals"]
    try:
        items = db.read_items(active_only=False)
    except Exception:
        items = []
    def cat(it):
        for t in (it.get("tags") or []):
            if t in _CATS:
                return t
        return "Admin"
    now = datetime.now(EASTERN)
    open_items = [i for i in items if i.get("status") == "open"]
    lines = ["BOARD: " + " · ".join(
        f"{c} {n}" for c in _CATS if (n := sum(1 for i in open_items if cat(i) == c)))]
    stale = []
    for i in open_items:
        if cat(i) == "Deals":
            try:
                age = (now - datetime.fromisoformat(i["ts"])).days
                if age >= 7:
                    stale.append(f"{i.get('text','')[:48]} ({age}d)")
            except Exception:
                pass
    if stale:
        lines.append("STALE DEALS (no touch 7+ days): " + "; ".join(stale[:5]))
    # Wins from memory (the win-logger writes "Deal won:" / "Goal reached:" facts)
    try:
        wins = [f for f in db.read_facts_full()
                if not f.get("invalid_at")
                and (f.get("text", "").startswith("Deal won:") or f.get("text", "").startswith("Goal reached:"))
                and (now - datetime.fromisoformat(f["ts"]).astimezone(EASTERN)).days <= 7]
        if wins:
            lines.append("WINS THIS WEEK: " + " | ".join(w["text"][:70] for w in wins[:5]))
    except Exception:
        pass
    return "\n".join(lines)


async def generate_brief(kind: str = "morning") -> str:
    """Build one brief and deliver it: thread + summary marker. Returns the text."""
    from . import db
    try:
        stats = await asyncio.to_thread(_board_stats)
        events_raw, wx, convo = await asyncio.gather(
            asyncio.to_thread(get_events_structured, 2 if kind == "eod" else 1),
            get_weather(),
            asyncio.to_thread(_unified_thread, 16),
            return_exceptions=True,
        )
        def ok(v, d):
            return d if isinstance(v, Exception) else v
        now = datetime.now(EASTERN)
        events = ok(events_raw, [])
        today_str = now.strftime("%Y-%m-%d")
        sched = _format_today_schedule([e for e in events if e.get("date") == today_str], now)
        tomorrow_block = ""
        if kind == "eod":
            tom = [e for e in events if e.get("date") != today_str][:4]
            tomorrow_block = "\n\nTOMORROW'S FIRST EVENTS:\n" + (
                "\n".join(f"- {e.get('time','')} {e.get('title','')}" for e in tom)
                or "(nothing scheduled yet)")
        mem_all = await asyncio.to_thread(brain.read_memory)
        goals = [f for f in mem_all if "goal" in f.lower() and not f.startswith("ACE SELF-NOTE")][:6]
        self_notes = [f for f in mem_all if f.startswith("ACE SELF-NOTE")][-3:]
        # Brief feedback steering — Brady's 👍/👎 votes tune tomorrow's brief
        fb = await asyncio.to_thread(db.latest_summary, "brief_feedback")
        fb_line = ""
        if fb.get("text", "").endswith(":down"):
            fb_line = "\n\nNOTE: Brady thumbed-down the last brief — tighten it, drop filler, sharper priorities."
        elif fb.get("text", "").endswith(":up"):
            fb_line = "\n\nNOTE: Brady liked the last brief — keep this style."
        client = _anthropic()
        if kind == "morning":
            ask = (
                "Write Brady's MORNING BRIEF as Ace — his JARVIS-style chief of staff waking up "
                "with him. Punchy, warm, zero fluff, ~140 words max. Structure: (1) one-line "
                "greeting with the weather beat; (2) TODAY — his schedule turned into a game "
                "plan, top 3 moves in priority order; (3) BUSINESS PULSE — one tight line from "
                "the board stats (deals moving, anything stale to touch, wins momentum); "
                "(4) one EMD-goal push line. Plain text, short lines, no markdown headers."
            )
        else:
            ask = (
                "Write Brady's END-OF-DAY RECAP as Ace. Warm, brief, ~110 words max. Structure: "
                "(1) what got DONE today (from the thread/board); (2) what carries to tomorrow "
                "(top 2-3, from open board items + tomorrow's first event); (3) one genuine "
                "one-line push tied to his goals. Plain text, no markdown headers."
            )
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": (
                f"{ask}\n\nCURRENT TIME: {now.strftime('%A, %B %d, %Y — %-I:%M %p')} ET\n\n"
                f"WEATHER: {_format_weather(ok(wx, {}))}\n\nTODAY'S SCHEDULE:\n{sched}{tomorrow_block}\n\n"
                f"{stats}\n\nHIS GOALS:\n" + ("\n".join(f"- {g}" for g in goals) or "(none)")
                + ("\n\nACE'S OWN GROWTH NOTES (mention max ONE, casually, only if morning):\n"
                   + "\n".join(f"- {n}" for n in self_notes) if self_notes else "")
                + fb_line
                + "\n\nRECENT THREAD (for what happened):\n" + _format_thread(ok(convo, [])))}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if not text:
            return ""
        label = "☀ MORNING BRIEF" if kind == "morning" else "◈ EVENING RECAP"
        delivered = f"{label}\n\n{text}"
        await asyncio.to_thread(history.append, "assistant", delivered)
        await asyncio.to_thread(db.add_summary, now.strftime("%Y-%m-%d"), f"brief_{kind}")
        logger.info("brief delivered: %s (%d chars)", kind, len(text))
        # …and to the PHONE. The HUD push (publish_stage_event) only reaches an OPEN tab;
        # this is the one that lands on a locked screen. Fire-and-forget, threaded, and
        # completely inert until the VAPID keys are set — a brief never waits on it and
        # never fails because of it. Both brief paths (the loop and /brief/run) come
        # through here, so one call site covers morning and EOD.
        try:
            from .main import send_push
            send_push("ACE · Morning Brief" if kind == "morning" else "ACE · Evening Recap",
                      text, "/")
        except Exception as e:
            logger.warning("brief phone push skipped: %s", e)
        return delivered
    except Exception as e:
        logger.warning("generate_brief(%s) failed: %s", kind, e)
        return ""


async def generate_business_report() -> str:
    """THE 'HOW'S MY BUSINESS?' REPORT — on-demand deep snapshot: full pipeline by
    category with ages, wins (30d), goals vs pace, week ahead, and straight talk on
    what needs attention. Deterministic numbers first; the deep model writes the read."""
    from . import db
    try:
        now = datetime.now(EASTERN)
        items, facts, events, convo = await asyncio.gather(
            asyncio.to_thread(db.read_items, False),
            asyncio.to_thread(db.read_facts_full),
            asyncio.to_thread(get_events_structured, 7),
            asyncio.to_thread(_unified_thread, 12),
            return_exceptions=True,
        )
        def ok(v, d):
            return d if isinstance(v, Exception) else v
        items, facts, events = ok(items, []), ok(facts, []), ok(events, [])
        _CATS = ["Deals", "Agents", "Admin", "Networking", "Business", "Tech", "Personal", "Goals"]
        def cat(it):
            for t in (it.get("tags") or []):
                if t in _CATS:
                    return t
            return "Admin"
        lines = []
        for c in ["Deals", "Agents"]:
            rows = []
            for i in items:
                if i.get("status") == "open" and cat(i) == c:
                    try:
                        age = (now - datetime.fromisoformat(i["ts"])).days
                    except Exception:
                        age = "?"
                    rows.append(f"  - {i.get('text','')[:70]} [{age}d old]")
            lines.append(f"{c.upper()} ({len(rows)} open):\n" + ("\n".join(rows) or "  (none)"))
        goals = [i.get("text", "") for i in items if i.get("status") == "open" and cat(i) == "Goals"]
        lines.append("GOALS:\n" + ("\n".join(f"  - {g[:70]}" for g in goals) or "  (none)"))
        wins = []
        for f in facts:
            t = f.get("text", "")
            if (t.startswith("Deal won:") or t.startswith("Goal reached:")) and not f.get("invalid_at"):
                try:
                    if (now - datetime.fromisoformat(f["ts"]).astimezone(EASTERN)).days <= 30:
                        wins.append(f"  - {t[:75]}")
                except Exception:
                    pass
        lines.append("WINS (last 30 days):\n" + ("\n".join(wins) or "  (none logged yet)"))
        week = "\n".join(f"  - {e.get('date','')} {e.get('time','')} {e.get('title','')[:55]}"
                         for e in events[:14]) or "  (clear)"
        lines.append("WEEK AHEAD:\n" + week)
        client = _anthropic()
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=900,
            messages=[{"role": "user", "content": (
                "You are Ace giving Brady the straight 'how's my business looking' read — his "
                "JARVIS chief of staff who knows the whole book. From the REAL data below, write: "
                "(1) THE HEADLINE — one sentence, honest temperature of the business; "
                "(2) PIPELINE — what's moving, what's aging out (call out anything 10+ days old "
                "by name), where the money is this month; (3) TEAM — agent momentum, who needs a "
                "push; (4) GOALS — pace vs his targets (EMD especially), the gap in plain numbers "
                "where possible; (5) THE ONE MOVE — the single highest-leverage action right now. "
                "Punchy, ~220 words max, plain text with short section labels. Never invent "
                "numbers not in the data.\n\n"
                f"TODAY: {now.strftime('%A, %B %d, %Y')}\n\n" + "\n\n".join(lines)
                + "\n\nRECENT CONTEXT:\n" + _format_thread(ok(convo, [])))}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if text:
            delivered = f"📊 BUSINESS PULSE\n\n{text}"
            await asyncio.to_thread(history.append, "assistant", delivered)
            return delivered
        return ""
    except Exception as e:
        logger.warning("business report failed: %s", e)
        return ""


_brief_sent: dict = {}   # process-local claim: {kind: "YYYY-MM-DD"} — survives db outages


async def _brief_loop() -> None:
    """Fire the morning/EOD briefs at their Eastern times — once per day each, INSIDE a
    2-hour window (a late boot never sends a 'morning' brief at 11pm). Claim-first via a
    local guard + db marker so restarts, deploy overlaps, and db outages never double-send
    or spam-generate."""
    from . import db
    while True:
        try:
            await asyncio.sleep(60)
            if not db.enabled():
                continue
            now = datetime.now(EASTERN)
            today = now.strftime("%Y-%m-%d")
            cur = now.hour * 60 + now.minute
            for kind, (hh, mm) in _BRIEF_TIMES.items():
                if not (0 <= cur - (hh * 60 + mm) <= 120):
                    continue   # only within 2h of the target — missed windows are skipped
                if _brief_sent.get(kind) == today:
                    continue   # local claim (also caps retries if db reads fail)
                last = await asyncio.to_thread(db.latest_summary, f"brief_{kind}")
                if (last.get("text") or "") == today:
                    _brief_sent[kind] = today
                    continue   # already sent (other container / before restart)
                # CLAIM FIRST — before the LLM call — so a deploy-overlap twin can't double-send.
                _brief_sent[kind] = today
                if not await asyncio.to_thread(db.add_summary, today, f"brief_{kind}"):
                    continue   # db write failed: keep the local claim, try again tomorrow
                text = await generate_brief(kind)
                if text:
                    try:
                        from .main import publish_stage_event
                        await publish_stage_event("brief", {"kind": kind, "text": text})
                    except Exception:
                        pass
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ── AMBIENT WATCH — Ace notices things and speaks up UNASKED ────────────────────
# The other half of proactive: the briefs are scheduled, this one is ambient. Every pass
# diffs the world DETERMINISTICALLY first (email ids, event fingerprints, deal ages) and
# spends a model call ONLY when something actually moved — an idle afternoon costs nothing.
# The model's main job is to say NOTHING: an interruption Brady never asked for has to earn
# its place, and a chatty watcher gets muted, which kills the whole feature. Hence the caps.
_WATCH_INTERVAL = 30 * 60.0             # LEAN MODE: pass ~every 30 min (was 12 — caps still gate nudges)
_WATCH_WAKE = (7 * 60, 21 * 60 + 30)    # 7:00–21:30 Eastern — he is NEVER pinged outside this
_WATCH_MAX_DAY = 3                      # a 4th nudge in one day is noise, not help
_WATCH_GAP_SEC = 45 * 60                # ...and never two back-to-back
_WATCH_COLD_DAYS = 10                   # a Deal untouched this long is going cold
_watch_running = [False]
_watch_last_ts = [0.0]   # process-local floor on the gap, so a failed db read can't unmute him

# Sender words that say nothing about WHO wrote: without this "PFI Support" matches the board
# on "support" and every no-reply blast reads like a client getting in touch.
_WATCH_GENERIC = {
    "noreply", "reply", "team", "support", "info", "notifications", "notification", "news",
    "newsletter", "mail", "email", "admin", "service", "services", "customer", "account",
    "accounts", "sales", "billing", "alert", "alerts", "hello", "help", "update", "updates",
    "care", "office", "contact", "group", "response", "invoice", "receipt", "llc", "inc",
}


def _watch_known_sender(sender: str, vocab: set) -> bool:
    """True when a sender's name already shows up in Ace's own world (facts + board) — the
    line between 'a client just emailed' and 'a vendor blasted a list'."""
    import re
    return any(w in vocab for w in re.findall(r"[a-z0-9]+", (sender or "").lower())
               if len(w) >= 4 and w not in _WATCH_GENERIC)


def _watch_scan(inbox: list, events: list, items: list, facts: list, prior: dict, now: datetime):
    """The cheap deterministic half: what a person would actually NOTICE since the last pass.
    Returns (signals, state) — state is the fingerprint set handed to the next pass, so each
    thing is noticed exactly once no matter how many passes see it."""
    import re
    vocab = set()
    for t in list(facts) + [i.get("text", "") for i in items]:
        vocab.update(w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) >= 4)
    prior_mail = set(prior.get("emails") or [])
    prior_ev = dict(prior.get("events") or {})
    flagged = list(prior.get("flagged") or [])
    fset = set(flagged)
    signals = []

    for m in inbox:
        mid = m.get("id") or ""
        if not mid or mid in prior_mail or not _watch_known_sender(m.get("from", ""), vocab):
            continue   # a stranger's cold email is not worth his attention; a name he knows is
        signals.append(f"NEW EMAIL from {m.get('from', '?')} — \"{m.get('subject', '')}\" — "
                       f"{m.get('snippet', '')[:100]}")

    ev_state, horizon = {}, now + timedelta(hours=24)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for e in events:
        key = f"{e.get('title', '')}|{e.get('date', '')}"
        ev_state[key] = e.get("iso", "")
        try:
            start = datetime.fromisoformat(e["iso"])
        except Exception:
            continue
        # All-day items have a midnight start, so the clock window would always call them past.
        if e.get("all_day"):
            in_window = e.get("date", "") in (today_str, tomorrow_str)
        else:
            in_window = (now - timedelta(minutes=5)) <= start <= horizon
        if not in_window:
            continue
        was = prior_ev.get(key)
        if was is None:
            signals.append(f"CALENDAR ADDED: {e.get('day_label', '')} {e.get('time', '')} — "
                           f"{e.get('title', '')}")
        elif was != e.get("iso", ""):
            signals.append(f"CALENDAR MOVED: {e.get('title', '')} → {e.get('day_label', '')} "
                           f"{e.get('time', '')}")
        mins = int((start - now).total_seconds() // 60)
        if not e.get("all_day") and 0 <= mins <= 30 and key not in fset:
            signals.append(f"STARTS IN {mins} MIN: {e.get('time', '')} — {e.get('title', '')}")
            flagged.append(key)
            fset.add(key)

    for it in items:
        if "Deals" not in (it.get("tags") or []):
            continue
        key = "deal:" + str(it.get("id", ""))
        if key in fset:
            continue
        try:
            age = (now - datetime.fromisoformat(it["ts"])).days
        except Exception:
            continue
        if age >= _WATCH_COLD_DAYS:
            signals.append(f"DEAL GOING COLD: {it.get('text', '')[:70]} — {age} days untouched")
            flagged.append(key)
            fset.add(key)

    mail_state = [m.get("id") for m in inbox if m.get("id")]
    mail_state += sorted(prior_mail - set(mail_state))   # sorted → a stable blob, so state
    return signals, {                                    # writes happen only on real change
        "emails": mail_state[:80], "events": ev_state, "flagged": flagged[-60:],
    }


async def _watch_pass_once(force: bool = False, dry_run: bool = False) -> dict:
    """One ambient pass: deterministic diff → at most ONE model call → at most one nudge.
    Returns the decision (this is what /watch/run reports). force skips the quiet-hours and
    rate-limit gates for testing; dry_run decides but delivers nothing and keeps the state."""
    if _watch_running[0]:
        return {"nudged": False, "skipped": "busy"}
    _watch_running[0] = True
    try:
        from . import db
        if not db.enabled():
            return {"nudged": False, "skipped": "no db"}
        now = datetime.now(EASTERN)
        cur = now.hour * 60 + now.minute
        if not force and not (_WATCH_WAKE[0] <= cur <= _WATCH_WAKE[1]):
            return {"nudged": False, "skipped": "quiet hours"}
        today = now.strftime("%Y-%m-%d")
        # ONE marker carries both caps: its text is today's count, its ts is the last nudge.
        mark = await asyncio.to_thread(db.latest_summary, "nudge_count")
        sent_today = 0
        if (mark.get("text") or "").startswith(today):
            try:
                sent_today = int(mark["text"].split(":")[-1])
            except Exception:
                sent_today = _WATCH_MAX_DAY   # unreadable marker → assume spent and stay quiet
        last_ts = _watch_last_ts[0]
        if mark.get("ts"):
            try:
                last_ts = max(last_ts, datetime.fromisoformat(mark["ts"]).timestamp())
            except Exception:
                pass
        if not force:
            if sent_today >= _WATCH_MAX_DAY:
                return {"nudged": False, "skipped": "daily cap"}
            if (time.time() - last_ts) < _WATCH_GAP_SEC:
                return {"nudged": False, "skipped": "too soon"}

        inbox, events, items, facts, prior_raw = await asyncio.gather(
            asyncio.to_thread(get_inbox_structured, 10),
            asyncio.to_thread(get_events_structured, 2),
            asyncio.to_thread(db.read_items, False),
            asyncio.to_thread(db.read_facts),
            asyncio.to_thread(db.latest_summary, "watch_state"),
            return_exceptions=True,
        )

        def ok(v, d):
            return d if isinstance(v, Exception) or v is None else v

        inbox, events, facts = ok(inbox, []), ok(events, []), ok(facts, [])
        items = [i for i in ok(items, []) if i.get("status") == "open"]
        try:
            prior = json.loads(ok(prior_raw, {}).get("text") or "{}")
        except Exception:
            prior = {}
        signals, state = _watch_scan(inbox, events, items, facts, prior, now)
        if not prior:
            # First pass ever (or after a wipe) — everything looks new. Set the baseline and
            # stay silent; nobody wants a nudge storm the minute a deploy lands.
            if not dry_run:
                await asyncio.to_thread(db.add_summary, json.dumps(state, sort_keys=True), "watch_state")
            return {"nudged": False, "skipped": "baseline set", "signals": signals}
        blob = json.dumps(state, sort_keys=True)
        if not dry_run and blob != json.dumps(prior, sort_keys=True):
            # Consume the signals BEFORE deciding: whatever the model does with them, the same
            # email/event never gets re-argued every 12 minutes for the rest of the day.
            await asyncio.to_thread(db.add_summary, blob, "watch_state")
        if not signals:
            return {"nudged": False, "skipped": "nothing changed"}

        sched = _format_today_schedule([e for e in events if e.get("date") == today], now)
        board = "\n".join(f"- [{','.join(i.get('tags') or []) or 'untagged'}] {i.get('text', '')[:70]}"
                          for i in items[:25]) or "(empty)"
        thread = _format_thread(await asyncio.to_thread(_unified_thread, 8))
        client = _anthropic()
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=120,
            messages=[{"role": "user", "content": (
                "You are Ace's ambient watch — you noticed something and now you decide whether "
                "it is worth INTERRUPTING Brady for, unprompted, mid-work. Silence is the default "
                "and the right answer most of the time: he gets at most 3 of these in a day.\n"
                "Speak up only for what a sharp chief of staff would tap him on the shoulder for "
                "RIGHT NOW: a client or agent he knows sent something that needs him, a meeting "
                "that moved or starts within the hour with nothing prepped for it, a live deal "
                "going cold. Stay silent for newsletters and vendors, anything he obviously "
                "already knows (check the recent thread), anything that can wait for tonight's "
                "recap or tomorrow's brief, and anything you'd only say to look useful.\n"
                "If it clears that bar, reply with EXACTLY ONE sentence, 20 words max, in his "
                "register: direct, specific, names and numbers, no greeting, no 'just wanted to', "
                "no offer to help, no question. Otherwise reply with the single word NOTHING.\n\n"
                f"TIME: {now.strftime('%A, %B %-d — %-I:%M %p')} ET\n\n"
                "WHAT CHANGED SINCE THE LAST PASS:\n" + "\n".join(f"- {s}" for s in signals)
                + f"\n\nTODAY'S SCHEDULE:\n{sched}\n\nHIS OPEN BOARD:\n{board}"
                + f"\n\nRECENT THREAD (never repeat something already said here):\n{thread}")}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        text = text.split("\n")[0].strip().strip('"').strip()[:220]
        if not text or text.upper().startswith("NOTHING"):
            return {"nudged": False, "skipped": "not worth interrupting", "signals": signals}
        delivered = f"◈ HEADS UP — {text}"
        if dry_run:
            return {"nudged": False, "dry_run": True, "text": delivered, "signals": signals}
        # CLAIM FIRST — bump the counter before delivering, so a deploy-overlap twin that
        # decides the same thing in the same minute can't double-tap him.
        if not await asyncio.to_thread(db.add_summary, f"{today}:{sent_today + 1}", "nudge_count"):
            return {"nudged": False, "skipped": "claim failed", "signals": signals}
        _watch_last_ts[0] = time.time()
        await asyncio.to_thread(history.append, "assistant", delivered)
        try:
            from .main import publish_stage_event
            await publish_stage_event("nudge", {"text": delivered})
        except Exception:
            pass
        logger.info("nudge delivered (%d today): %s", sent_today + 1, text[:80])
        return {"nudged": True, "text": delivered, "count_today": sent_today + 1, "signals": signals}
    except Exception as e:
        logger.warning("watch pass failed: %s", e)
        return {"nudged": False, "error": str(e)}
    finally:
        _watch_running[0] = False


async def _watch_loop() -> None:
    """Sleeps FIRST so a boot or redeploy never fires a nudge before the caches are warm."""
    while True:
        try:
            await asyncio.sleep(_WATCH_INTERVAL)
            await _watch_pass_once()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _refresh_recap() -> None:
    """Keep the 'where we left off' recap warm in _CTX, and REGENERATE it (compaction) when it's
    stale and there are newer turns. So context COMPOUNDS: he opens every conversation knowing
    the decisions, open loops, and what's pending — never a cold reload. Background only; never
    on a turn's hot path. One at a time (_recap_running)."""
    if _recap_running[0]:
        return
    _recap_running[0] = True
    try:
        from . import db
        if not db.enabled():
            return
        latest = await asyncio.to_thread(db.latest_summary, "recap")
        if latest.get("text"):
            _CTX["recap"] = latest["text"]        # keep the current recap warm for voice
        fresh = False
        if latest.get("ts"):
            try:
                age = (datetime.now(EASTERN) - datetime.fromisoformat(latest["ts"])).total_seconds()
                fresh = age < _RECAP_TTL
            except Exception:
                fresh = False
        if fresh:
            return
        turns = await asyncio.to_thread(db.recent_turns, 60)
        if not turns or len(turns) < 4:
            return
        convo = "\n".join(f"{t.get('role')}: {(t.get('content') or '')[:300]}" for t in turns[-50:])
        client = _anthropic()
        resp = await client.messages.create(
            model=VOICE_MODEL, max_tokens=420,
            messages=[{"role": "user", "content": (
                "From this recent conversation, write Brady's assistant a tight 'where we left off' "
                "brief so he can pick up seamlessly. Cover: decisions made, what's IN PROGRESS or "
                "waiting on someone, open loops / promised follow-ups, and anything Brady said he "
                "finished. 4-8 short concrete bullet lines (names, deals). No preamble.\n\n"
                f"CONVERSATION:\n{convo}")}])
        recap = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if recap:
            await asyncio.to_thread(db.add_summary, recap, "recap")
            _CTX["recap"] = recap
            logger.info("recap regenerated (%d chars)", len(recap))
    except Exception as e:
        logger.warning("recap refresh failed: %s", e)
    finally:
        _recap_running[0] = False


def _recap_block() -> str:
    """The current recap for context (prefers the warm _CTX copy; falls back to a Postgres read)."""
    recap = _CTX.get("recap") or ""
    if not recap:
        try:
            from . import db
            if db.enabled():
                recap = (db.latest_summary("recap") or {}).get("text", "")
        except Exception:
            recap = ""
    return recap or "(no recap yet — this builds after your next few conversations)"


async def _fast_context() -> str:
    """Lean context for LOW-LATENCY voice, served from the pre-warmed cache (see _refresh_ctx).
    Formats the string from cached data + the LIVE clock, so CURRENT TIME is always correct
    while the heavy fetches never sit on the turn's critical path (the source of the timeouts).
    """
    now = datetime.now(EASTERN)
    if _CTX["ts"] == 0.0:
        # Cold (fresh boot, prime still in flight) → WAIT for the fetch, bounded, so the
        # first turn speaks from real context instead of a confidently empty one. The SSE
        # lead-in covers ElevenLabs while we wait; worst case we proceed lean at 8s.
        try:
            await asyncio.wait_for(_refresh_ctx(), timeout=8)
        except asyncio.TimeoutError:
            logger.warning("cold ctx wait >8s — proceeding lean this turn")
    elif (time.time() - _CTX["ts"]) > _CTX_TTL:
        # Stale → serve what we have NOW (fast); refresh in the background for the next turn.
        asyncio.create_task(_refresh_ctx())

    def ok(v, default):
        return default if isinstance(v, Exception) or v is None else v

    events = _CTX["events"]
    today_str = now.strftime("%Y-%m-%d")
    today_events = [e for e in events if e.get("date") == today_str]
    mem_list = _CTX["memory"]
    mem = "\n".join(f"- {m}" for m in mem_list) if mem_list else "(memory empty)"
    bank = _CTX["bank"]
    wx = _CTX["wx"]
    # Continuity: the unified thread (voice + chat, date-stamped) so voice remembers
    # today's typed turns too — not just its own call and not the stale Telegram window.
    convo_str = _format_thread(_CTX["convo"][-24:])   # voice: wider memory window (was 12)
    # HARD DATE ANCHOR (Brady: voice "keeps forgetting what day it is"). The voice brain is
    # the fast/small model — it HAS the date but a passive line let it drift on live calls.
    # State it as ground truth + an explicit instruction so it can never guess or ask.
    now_line = now.strftime("%A, %B %d, %Y — %-I:%M %p")
    return "\n".join([
        f"⏰ RIGHT NOW IT IS: {now_line} (US Eastern). This is the current date and time — "
        f"it is TODAY. If Brady asks what day, date, or time it is, answer with THIS exactly. "
        f"Never guess the date, never say you're unsure, never ask him what day it is — you "
        f"always know, it is stated right here and refreshed every turn.",
        "",
        "ACE MEMORY (durable facts about Brady and PFI):",
        mem,
        "",
        "WHERE YOU LEFT OFF (recap of your recent conversations — pick up from here, don't re-ask):",
        _recap_block(),
        "",
        f"TODAY'S SCHEDULE ({now.strftime('%A, %B %-d')} — this is his calendar for TODAY, "
        f"relative to the current time above; answer 'what's on my calendar / what's next' "
        f"from this, never say you can't see it):",
        _format_today_schedule(today_events, now),
        "",
        "CALENDAR — last week through the next 3 weeks (past events for reference, "
        "upcoming for planning; answer range questions from this directly):",
        _format_calendar_window(events, now),
        "",
        "HIS TASK BOARD (Ace's OWN store — THE task & pipeline system, what Brady sees in his "
        "Command panel. To mark one done, match what he says to an item below and call "
        "update_item with its id; capture new tasks with capture_item):",
        _format_daybank(ok(bank, [])),
        "",
        "WEATHER RIGHT NOW:",
        _format_weather(ok(wx, {})),
        "",
        "RECENT THREAD (what you and Brady have been discussing lately, across the HUD, "
        "voice and Telegram — use it for continuity):",
        convo_str,
        "",
        "(VOICE MODE — you are speaking out loud to Brady. Everything under LIVE CONTEXT above is "
        "live and in front of you — ANSWER FROM IT directly and confidently; never say you \"can't "
        "see\" something that's here, and never tell him to open a screen for it. You ALSO have your "
        "tools on this call: use display_card to put things on his screen, get_calendar_range / "
        "search_gmail / read_gmail to pull anything not already in context, and create_calendar_event, "
        "delete_calendar_event, capture_item (add to his task board), update_item (mark a board "
        "item done — match his words to an id in HIS TASK BOARD above), send_email, draft_email, "
        "search_drive to ACT — actually do these, then tell him it's done. For anything you have no "
        "spoken tool for — CREATING or EDITING a Google Doc, building or updating a Sheet or a "
        "Slides deck, or making a shareable link — call build_on_screen with the full instruction, "
        "then tell him in one short sentence to watch his screen; NEVER say you can't do it. "
        "Sending and deleting are "
        "GATED: the tool will come back asking for confirmation — say in one short sentence exactly "
        "what you're about to do, wait for his yes, then call it again with confirmed true. "
        "Keep spoken replies short and natural — a sentence or two, no lists or markdown. When "
        "it's just conversation — Brady, or a friend he puts on the mic — BE good company: warm, "
        "a little personality, react to what they actually said, carry the thread, and toss back "
        "a natural question so it flows; never clipped or robotic, and don't narrate tools. "
        "CALL RITUALS: (1) WAKE-UP — when Brady opens with a morning greeting ('good morning, "
        "Ace' / 'morning'), come online like JARVIS: greet him back for the time of day, then ONE "
        "tight line with the weather beat and his NEXT event from the schedule above. Two short "
        "sentences max, then stop — don't dump the whole day. (2) PAUSE — if he says 'hold on', "
        "'pause', 'one sec', 'meeting mode', or starts talking to someone else: call skip_turn "
        "with NO words if you have it (otherwise say only 'Standing by.') and then stay silent — "
        "do not speak again, and never respond to background conversation, until he addresses you "
        "directly. (3) SIGN-OFF — when he's clearly done ('goodnight, Ace', 'that's all for "
        "tonight'): one short, warm sign-off line, then call end_call if you have it to hang up. "
        "Never call end_call in any other situation.)",
    ])


STAGE_TOOLS = [t for t in tools.TOOLS if t["name"] in tools.UI_TOOLS]
VOICE_TOOLS = [t for t in tools.TOOLS if t["name"] not in _VOICE_TOOL_DENY] + [tools.BUILD_ON_SCREEN]
# Picking which card to show is a simple call — run it on Haiku, not Opus, so the stage
# pass adds minimal cost/latency on top of every voice turn.
STAGE_MODEL = os.environ.get("ACE2_STAGE_MODEL", "claude-haiku-4-5-20251001")

STAGE_NUDGE = (
    "\n\n---\nSTAGE MODE: Brady is on a live VOICE call — he is talking, not reading. "
    "Your ONLY job right now is the screen. Call display_card (or open_url) when he asks "
    "to see something, or when a card materially helps what he just said (e.g. he asks "
    "about his schedule → show 'timeline' or 'calendar'; his to-dos → 'daybank'). If "
    "nothing should appear on screen, call NO tool at all. Do not produce spoken text — a "
    "separate process handles the reply; you only decide the visuals."
)


async def stage_pass(user_text: str, emit, prior=None):
    """Off-critical-path DISPLAY decision for a live voice turn: pick a card (if any) and
    push it to Brady's browser over the app WebSocket. Display-only tools, no thinking, one
    non-streaming round-trip. It NEVER writes to the ElevenLabs SSE stream, so it cannot
    reintroduce the first-token 'LLM Cascade Error' — that's the whole point of keeping it
    separate from the spoken reply in stream_turn(fast=True)."""
    user_text = (user_text or "").strip()
    if not user_text:
        return
    try:
        ctx = await _fast_context()
        system = build_system_prompt() + STAGE_NUDGE + "\n\n---\nLIVE CONTEXT\n" + ctx
        messages = await _load_messages(user_text, prior)
        client = _anthropic()
        final = await client.messages.create(
            model=STAGE_MODEL, max_tokens=512, system=system, messages=messages, tools=STAGE_TOOLS,
        )
        for block in final.content:
            if getattr(block, "type", "") == "tool_use" and block.name in tools.UI_TOOLS:
                await _run_ui_tool(block.name, dict(block.input), emit)
    except Exception as e:
        logger.warning("stage_pass failed: %s", e)


async def stream_turn(user_text: str, emit, prior=None, fast=False, extra_tools=None):
    """Run one Ace turn, emitting WS events via `emit(type, payload)` (async).

    prior: the conversation so far. The WS handler passes its per-connection
    transcript (shared Telegram window + this session's turns) so Ace actually
    remembers the conversation he's in; the voice adapter passes ElevenLabs'
    messages. None = fall back to the shared window only (HTTP one-shots).

    fast: low-latency path for live voice — lean context + low effort so Ace
    starts speaking quickly instead of stalling behind a full data prefetch.

    extra_tools: PASSTHROUGH tool schemas (Anthropic format) the CALLER executes,
    not us — ElevenLabs system tools (end_call, skip_turn, …) forwarded by the
    voice adapter. When the model calls one we emit `el_tool` and END the turn;
    ElevenLabs performs the action (hang up / yield the turn). Voice-only.

    Returns the reply text so the caller can append it to its transcript.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        await emit("error", {"text": "Empty message"})
        return ""

    try:
        ctx = await (_fast_context() if fast else _live_context())
        system = build_system_prompt() + "\n\n---\nLIVE CONTEXT\n" + ctx
        messages = await _load_messages(user_text, prior)
    except Exception as e:
        logger.error("context build failed: %s", e)
        await emit("error", {"text": f"⚠️ Couldn't reach your data: {e}"})
        return ""

    full_reply = []
    confirmations = []
    handed_off = False   # a build_on_screen handoff already fired this turn — don't double-fire
    blocked_counts: dict = {}   # per-tool confirm-gate blocks THIS turn (2nd+ gets the STOP message)
    passthrough = {t["name"] for t in (extra_tools or [])}
    passthrough_called = False   # an el_tool fired (end_call/skip_turn) — the platform takes over
    # One monotonic id per real user turn — the confirm gate uses it to tell "Brady
    # spoke again" from "the model looped inside this same turn." Immune to the
    # transcript cap that a message-count would deadlock on.
    turn_id = _next_turn_id()

    try:
        client = _anthropic()
        if fast:
            # Voice = SPEED, because ElevenLabs cuts the call if the first token is late and
            # goes silent if a tool stalls. So: Haiku (fastest first token), NO extended
            # thinking, and NATIVE tools ONLY — no MCP (typed keeps the full MCP surface).
            # PROMPT CACHING: the static prompt + native tool schemas are byte-stable, so
            # mark them as a cached prefix — Haiku stops re-paying ~6k tokens of prefill
            # per turn, pulling first-token well under the lead-in threshold.
            voice_tools = [dict(t) for t in VOICE_TOOLS] + list(extra_tools or [])
            voice_tools[len(VOICE_TOOLS) - 1]["cache_control"] = {"type": "ephemeral"}
            cached_system = [
                {"type": "text", "text": build_system_prompt(),
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "\n\n---\nLIVE CONTEXT\n" + ctx},
            ]
            stream_kwargs = dict(model=VOICE_MODEL, max_tokens=1500, system=cached_system,
                                 messages=messages, tools=voice_tools)
        else:
            # MCP folds into the TYPED loop only (fetched once, cached; empty when dormant).
            # WEB_SEARCH (2026-07-31) rides here too — Anthropic executes it server-side, so
            # the dispatch loop below never sees it (its blocks are 'server_tool_use', which
            # the != 'tool_use' guard already skips). Typed only: voice stays native-fast.
            # LEAN MODE prompt caching (2026-08-03): same trick as voice — the static prompt
            # + tool schemas are byte-stable, so mark them as a cached prefix. Once warm,
            # every typed turn re-reads ~10k prefix tokens at 10% price instead of full rate.
            mcp_schemas = await mcp_client.tool_schemas()
            typed_tools = [dict(t) for t in tools.TOOLS] + list(mcp_schemas) + [dict(tools.WEB_SEARCH)]
            typed_tools[-1]["cache_control"] = {"type": "ephemeral"}
            typed_system = [
                {"type": "text", "text": build_system_prompt(),
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "\n\n---\nLIVE CONTEXT\n" + ctx},
            ]
            stream_kwargs = dict(
                model=MODEL, max_tokens=MAX_TOKENS, system=typed_system, messages=messages,
                tools=typed_tools,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
            )
    except Exception as e:
        # Setup failure (bad key, MCP registry down) must NEVER die silently — a silent
        # death made every voice turn end in the same canned fallback line, which read
        # as Ace stuck in a loop. Emit the error (voice speaks one fixed line for it).
        logger.error("stream_turn setup failed: %s", e)
        await emit("error", {"text": f"⚠️ {e}"})
        return ""

    try:
        for _ in range(MAX_TOOL_ITERS):
            turn_text = []
            async with client.messages.stream(**stream_kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        turn_text.append(event.delta.text)
                        await emit("delta", {"text": event.delta.text})
                final = await stream.get_final_message()

            if turn_text:
                full_reply.append("".join(turn_text))

            # Web-search receipts: the API already executed these server-side — nothing to
            # run, but Brady should SEE that Ace looked something up (and what he searched).
            for _b in final.content:
                if getattr(_b, "type", "") == "server_tool_use":
                    try:
                        _q = (dict(getattr(_b, "input", {}) or {}).get("query") or "")[:60]
                    except Exception:
                        _q = ""
                    await emit("tool", {"name": "web_search",
                                        "label": "SEARCHED THE WEB" + (f": {_q}" if _q else ""),
                                        "status": "done", "ui": False})

            if final.stop_reason == "max_tokens":
                # Ran out of room (likely deep in thinking) before finishing —
                # never silently claim success or drop a pending tool call.
                logger.warning("turn hit max_tokens (%s); reply so far %d chars",
                               MAX_TOKENS, sum(len(t) for t in full_reply))
                if not "".join(full_reply).strip():
                    full_reply.append("I got a bit tangled working that through — ask me again, and I'll keep it tighter.")
                break

            if final.stop_reason != "tool_use":
                break

            # Execute every tool call in this turn, feed all results back together.
            messages.append({"role": "assistant", "content": final.content})
            tool_results = []
            for block in final.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                if block.name in passthrough:
                    # An ElevenLabs system tool (end_call, skip_turn, …). We don't execute it —
                    # we relay the call back through the SSE stream and ElevenLabs performs the
                    # action (hangs up / yields the turn). The turn ends here on our side.
                    await emit("el_tool", {"name": block.name, "args": dict(block.input), "id": block.id})
                    passthrough_called = True
                    continue
                if block.name == "build_on_screen":
                    # Voice can't author Docs/Sheets/Slides itself — hand the task to the HUD,
                    # which runs it as a normal typed turn with the full MCP toolset (and the
                    # usual on-screen confirm flow). Ace just tells Brady to watch his screen.
                    # Handled here, before any status narration or confirm gate. The spoken
                    # line MUST match what actually happened, so branch on the real outcome:
                    # empty request, already-dispatched, no screen connected, or success.
                    req = (dict(block.input).get("request") or "").strip()
                    if not req:
                        result = ("No task text came through. Ask Brady in one short sentence "
                                  "what he wants built, then call build_on_screen again.")
                    elif handed_off:
                        result = ("Already sent that to the screen — just tell Brady to watch "
                                  "it. Do NOT send it again.")
                    else:
                        # emit('handoff') returns how many HUDs received it (openai_compat path).
                        reached = await emit("handoff", {"message": req})
                        if reached == 0:
                            result = ("No screen is connected, so this can't run on the HUD. Tell "
                                      "Brady in one short sentence to open the Ace app on a screen, "
                                      "then ask again. Do NOT say it's being built — it isn't.")
                        else:
                            handed_off = True
                            result = ("Sent to the on-screen assistant — it's building this on the "
                                      "screen now with the full Workspace tools. In ONE short "
                                      "sentence, tell Brady to watch his screen, and that if it "
                                      "asks him to confirm anything, to okay it there. Don't do it "
                                      "yourself or read the request back.")
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                    continue
                is_ui = block.name in tools.UI_TOOLS
                # Gate BEFORE any "running" narration — a blocked action must never be
                # spoken/painted as if it happened (it's about to be asked, not done).
                blocked, use_args = _confirm_gate(block.name, dict(block.input), turn_id)
                if blocked:
                    # No action pill/narration; just a keep-alive token so a voice
                    # turn's confirmation question doesn't stall behind a silent round-trip.
                    await emit("hold", {"name": block.name})
                    blocked_counts[block.name] = blocked_counts.get(block.name, 0) + 1
                    # Re-blocked in the SAME turn = the model is retrying with confirmed:true
                    # in the same breath; without a hard STOP it churns silent round-trips
                    # until MAX_TOOL_ITERS while the caller hears only continuers.
                    result = _CONFIRM_MSG if blocked_counts[block.name] == 1 else _CONFIRM_STOP_MSG
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                    continue
                label = tools.TOOL_LABELS.get(block.name) or \
                    block.name.removeprefix("mcp_").replace("_", " ").upper()
                await emit("tool", {"name": block.name, "label": label, "status": "running", "ui": is_ui})
                if is_ui:
                    result = await _run_ui_tool(block.name, use_args, emit)
                elif mcp_client.is_mcp_tool(block.name):
                    result = await mcp_client.call(block.name, use_args)
                else:
                    result = await asyncio.to_thread(tools.execute, block.name, use_args)
                await emit("tool", {"name": block.name, "label": label, "status": "done", "ui": is_ui})
                if not is_ui:
                    await emit("confirmation", {"text": result})
                    confirmations.append(result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            if passthrough_called:
                # ElevenLabs is taking the action (hang-up / turn-yield) — no further model
                # round-trip; anything Ace wanted to say already streamed.
                break
            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning("hit MAX_TOOL_ITERS for: %s", user_text[:80])

        reply = "".join(full_reply).strip()
        if fast and not reply and not passthrough_called:
            # An empty completion makes ElevenLabs treat the turn as an LLM failure
            # and cascade — always give voice something to say.
            reply = "I'm here — say that again for me?"
        await emit("final", {"text": reply})

        # Persist to 2.0's OWN history (best-effort; never blocks the reply). ONLY the real
        # exchange — raw tool-result strings used to be stored as things "Ace said," and
        # those calendar dumps / ⚠️ warnings resurfaced in the RECENT THREAD, nudging the
        # voice model into status-report babble. The reply already summarizes what was done.
        try:
            await asyncio.to_thread(history.append, "user", user_text)
            if reply:
                await asyncio.to_thread(history.append, "assistant", reply)
        except Exception as e:
            logger.warning("history persist skipped: %s", e)

        return reply

    except Exception as e:
        logger.error("stream_turn error: %s", e)
        await emit("error", {"text": f"⚠️ {e}"})
        return ""
