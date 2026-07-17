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
from datetime import datetime

import pytz
from anthropic import AsyncAnthropic

from . import brain, history, tools
from .integrations.calendar_api import (
    get_calendar_events,
    get_events_structured,
    get_tomorrow_events,
)
from .integrations.tasks_api import get_tasks, get_tasks_structured
from .integrations.weather import get_weather
from .system_prompt import build_system_prompt

logger = logging.getLogger("ace2.chat")

EASTERN = pytz.timezone("America/New_York")
MODEL = os.environ.get("ACE2_MODEL", "claude-opus-4-8")
EFFORT = os.environ.get("ACE2_EFFORT", "medium")   # low|medium|high|xhigh|max
MAX_TOKENS = int(os.environ.get("ACE2_MAX_TOKENS", "16000"))  # ceiling covers thinking+tools+prose; only billed if used
MAX_TOOL_ITERS = 8

_client = None


def _anthropic() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


async def _live_context() -> str:
    """Fetch memory + today/tomorrow calendar + tasks concurrently, format once."""
    memory, today, tomorrow, tasks = await asyncio.gather(
        asyncio.to_thread(brain.read_memory),
        asyncio.to_thread(get_calendar_events, 1),
        asyncio.to_thread(get_tomorrow_events),
        asyncio.to_thread(get_tasks),
        return_exceptions=True,
    )

    def ok(v, default):
        return default if isinstance(v, Exception) else v

    now = datetime.now(EASTERN)
    mem_list = ok(memory, [])
    mem = "\n".join(f"- {m}" for m in mem_list) if mem_list else "(memory empty)"
    parts = [
        f"CURRENT TIME (Eastern): {now.strftime('%A, %B %d, %Y — %-I:%M %p')}",
        "",
        "ACE MEMORY (what you know about Brady and PFI):",
        mem,
        "",
        "TODAY'S CALENDAR:",
        ok(today, "(unavailable)") or "(nothing today)",
        "",
        "TOMORROW:",
        ok(tomorrow, "(unavailable)") or "(nothing tomorrow)",
        "",
        "OPEN TASKS:",
        ok(tasks, "(unavailable)") or "(inbox zero)",
    ]
    return "\n".join(parts)


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
        return {"events": await asyncio.to_thread(get_events_structured, 7)}
    if panel == "tasks":
        return {"tasks": await asyncio.to_thread(get_tasks_structured)}
    if panel == "weather":
        return await get_weather()
    if panel == "memory":
        return {"memories": await asyncio.to_thread(brain.read_memory)}
    return {}


async def _run_ui_tool(name: str, tool_input: dict, emit) -> str:
    """Execute a UI tool: the 'action' is an event the frontend materializes."""
    if name == "display_card":
        panel = (tool_input.get("panel") or "").strip().lower()
        try:
            data = await _card_payload(panel)
            await emit("card", {"panel": panel, "data": data})
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


async def stream_turn(user_text: str, emit, prior=None):
    """Run one Ace turn, emitting WS events via `emit(type, payload)` (async).

    prior: the conversation so far. The WS handler passes its per-connection
    transcript (shared Telegram window + this session's turns) so Ace actually
    remembers the conversation he's in; the voice adapter passes ElevenLabs'
    messages. None = fall back to the shared window only (HTTP one-shots).

    Returns the reply text so the caller can append it to its transcript.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        await emit("error", {"text": "Empty message"})
        return ""

    try:
        system = build_system_prompt() + "\n\n---\nLIVE CONTEXT\n" + await _live_context()
        messages = await _load_messages(user_text, prior)
    except Exception as e:
        logger.error("context build failed: %s", e)
        await emit("error", {"text": f"⚠️ Couldn't reach your data: {e}"})
        return ""

    client = _anthropic()
    full_reply = []
    confirmations = []

    try:
        for _ in range(MAX_TOOL_ITERS):
            turn_text = []
            async with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools.TOOLS,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
            ) as stream:
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
