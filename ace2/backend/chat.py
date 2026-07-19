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
import logging
import os
from datetime import datetime, timedelta

import pytz
from anthropic import AsyncAnthropic

from . import brain, daybank, history, tools
from .integrations.calendar_api import (
    get_events_structured,
    get_tomorrow_events,
)
from .integrations.tasks_api import get_gmail_summary, get_inbox_structured, get_tasks, get_tasks_structured
from .integrations.weather import get_weather
from .system_prompt import build_system_prompt

logger = logging.getLogger("ace2.chat")

EASTERN = pytz.timezone("America/New_York")
MODEL = os.environ.get("ACE2_MODEL", "claude-opus-4-8")
# Live VOICE replies run on the FASTEST model, not Opus — conversational snappiness
# (time-to-first-word) matters far more than depth per spoken sentence, and Opus's
# thinking latency is the main thing that makes voice feel laggy. Typed stays on MODEL.
# Bump to claude-sonnet-5 via env if voice needs more reasoning per turn.
VOICE_MODEL = os.environ.get("ACE2_VOICE_MODEL", "claude-haiku-4-5-20251001")
# On voice Ace gets the full toolset so he can ACT — including delete_calendar_event (Brady
# explicitly granted it 2026-07-19 for reschedules). The one hold-out is send_email (outbound
# to third parties) until the confirm-before-send guardrail exists. Built once for cache stability.
_VOICE_TOOL_DENY = {"send_email"}
EFFORT = os.environ.get("ACE2_EFFORT", "medium")   # low|medium|high|xhigh|max
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
    memory, cal_all, tasks, bank, inbox, wx = await asyncio.gather(
        asyncio.to_thread(brain.read_memory),
        asyncio.to_thread(get_events_structured, 21, 7),  # last week → next 3 weeks
        asyncio.to_thread(get_tasks),
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
        "OPEN TASKS:",
        ok(tasks, "(unavailable)") or "(inbox zero)",
        "",
        "DATA BANK (what you're already tracking for Brady — his to-dos/commitments/"
        "notes; each has an id you pass to update_item to complete it):",
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
    lines = []
    for it in open_items:
        due = f" (due {it['due']})" if it.get("due") else ""
        lines.append(f"- [{it.get('id','?')}] {it.get('kind','note')}: {it.get('text','')}{due}")
    for it in done_today:
        lines.append(f"- [{it.get('id','?')}] ✓ done: {it.get('text','')}")
    return "\n".join(lines)


async def _load_messages(user_text: str, prior=None) -> list:
    """Conversation for the API, sanitized to {role,content} + this turn.

    prior=None (HUD chat): read the shared Telegram window for continuity — 2.0
    reads it, never writes it. prior given (voice adapter): use the caller's
    conversation verbatim; ElevenLabs sends the full turn history each call, so
    that IS the source of truth for intra-call memory.
    """
    if prior is None:
        prior = await asyncio.to_thread(brain.read_shared_conversation)
    msgs = brain.sanitize_for_api(prior)
    if not msgs or msgs[-1].get("content") != user_text:
        msgs.append({"role": "user", "content": user_text})
    return msgs


async def _card_payload(panel: str):
    """Structured data for a display_card call. Concurrent, graceful."""
    if panel == "calendar":
        return {"events": await asyncio.to_thread(get_events_structured, 21, 7)}
    if panel == "timeline":
        # Today only; the frontend computes the NOW line against the live clock.
        return {"events": await asyncio.to_thread(get_events_structured, 1)}
    if panel == "tasks":
        return {"tasks": await asyncio.to_thread(get_tasks_structured)}
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


async def _fast_context() -> str:
    """Lean context for LOW-LATENCY voice: memory + time + today's schedule +
    data bank + the recent conversation thread (for continuity).

    The full `_live_context()` also fans out to Gmail, Tasks and weather every turn
    — several seconds before Ace can speak, which is fatal on a live call. Voice
    skips those (Ace has no tools on this channel anyway) but DOES carry memory,
    today's schedule, the tracked data bank, and the last few turns of the ongoing
    thread so it isn't a cold start — all fetched concurrently to stay fast.
    """
    now = datetime.now(EASTERN)
    memory, cal_all, bank, convo, wx = await asyncio.gather(
        asyncio.to_thread(brain.read_memory),
        asyncio.to_thread(get_events_structured, 21, 7),  # last week → next 3 weeks
        asyncio.to_thread(daybank.read_items, True),
        asyncio.to_thread(brain.read_shared_conversation),
        get_weather(),
        return_exceptions=True,
    )

    def ok(v, default):
        return default if isinstance(v, Exception) else v

    events = ok(cal_all, [])
    today_str = now.strftime("%Y-%m-%d")
    today_events = [e for e in events if e.get("date") == today_str]
    mem_list = ok(memory, [])
    mem = "\n".join(f"- {m}" for m in mem_list) if mem_list else "(memory empty)"
    # Continuity: the last few turns of the ongoing thread (HUD + voice + Telegram)
    # so voice remembers what we were just talking about — not a cold start each call.
    thread = brain.sanitize_for_api(ok(convo, []))[-10:]
    convo_str = "\n".join(f"{m['role']}: {m['content'][:280]}" for m in thread) if thread else "(no earlier conversation on record)"
    return "\n".join([
        f"CURRENT TIME (Eastern): {now.strftime('%A, %B %d, %Y — %-I:%M %p')}",
        "",
        "ACE MEMORY (durable facts about Brady and PFI):",
        mem,
        "",
        "TODAY'S SCHEDULE (relative to the current time above):",
        _format_today_schedule(today_events, now),
        "",
        "CALENDAR — last week through the next 3 weeks (past events for reference, "
        "upcoming for planning; answer range questions from this directly):",
        _format_calendar_window(events, now),
        "",
        "DATA BANK (commitments / to-dos / deals you're already tracking for Brady):",
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
        "delete_calendar_event, capture_item, update_item, add_task, draft_email, search_drive to ACT "
        "— actually do these, then tell him it's done. (The one thing you can't do by voice is SEND "
        "email — offer to draft instead.) Keep spoken replies short and natural — a sentence or two, "
        "no lists or markdown; if you're doing something, say so in a few words while it happens.)",
    ])


STAGE_TOOLS = [t for t in tools.TOOLS if t["name"] in tools.UI_TOOLS]
VOICE_TOOLS = [t for t in tools.TOOLS if t["name"] not in _VOICE_TOOL_DENY]
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


async def stream_turn(user_text: str, emit, prior=None, fast=False):
    """Run one Ace turn, emitting WS events via `emit(type, payload)` (async).

    prior: the conversation so far. The WS handler passes its per-connection
    transcript (shared Telegram window + this session's turns) so Ace actually
    remembers the conversation he's in; the voice adapter passes ElevenLabs'
    messages. None = fall back to the shared window only (HTTP one-shots).

    fast: low-latency path for live voice — lean context + low effort so Ace
    starts speaking quickly instead of stalling behind a full data prefetch.

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

    client = _anthropic()
    full_reply = []
    confirmations = []

    # Voice (fast) path: NO tools, NO extended thinking, tight token cap — the
    # model must lead with SPOKEN TEXT so the first token streams inside
    # ElevenLabs' custom-LLM first-token deadline. Leading with a tool call or a
    # thinking block = no text in time = "LLM Cascade Error" (the call fails and
    # Ace never replies). The typed path keeps the full tooled + thinking loop.
    if fast:
        # Voice: fast model + real tools (so Ace can ACT by voice), but NO extended thinking.
        # openai_compat streams a short filler first, so leading with a tool call no longer
        # starves ElevenLabs' first-token deadline (what used to cause the cascade error).
        stream_kwargs = dict(model=VOICE_MODEL, max_tokens=1500, system=system,
                             messages=messages, tools=VOICE_TOOLS)
    else:
        stream_kwargs = dict(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages,
            tools=tools.TOOLS, thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
        )

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
                label = tools.TOOL_LABELS.get(block.name, block.name.upper())
                await emit("tool", {"name": block.name, "label": label, "status": "running"})
                if block.name in tools.UI_TOOLS:
                    result = await _run_ui_tool(block.name, dict(block.input), emit)
                else:
                    result = await asyncio.to_thread(tools.execute, block.name, dict(block.input))
                await emit("tool", {"name": block.name, "label": label, "status": "done"})
                if block.name not in tools.UI_TOOLS:
                    await emit("confirmation", {"text": result})
                    confirmations.append(result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning("hit MAX_TOOL_ITERS for: %s", user_text[:80])

        reply = "".join(full_reply).strip()
        if fast and not reply:
            # An empty completion makes ElevenLabs treat the turn as an LLM failure
            # and cascade — always give voice something to say.
            reply = "I'm here — say that again for me?"
        await emit("final", {"text": reply})

        # Persist to 2.0's OWN history (best-effort; never blocks the reply).
        try:
            await asyncio.to_thread(history.append, "user", user_text)
            if reply:
                await asyncio.to_thread(history.append, "assistant", reply)
            for c in confirmations:
                await asyncio.to_thread(history.append, "assistant", c)
        except Exception as e:
            logger.warning("history persist skipped: %s", e)

        return reply

    except Exception as e:
        logger.error("stream_turn error: %s", e)
        await emit("error", {"text": f"⚠️ {e}"})
        return ""
