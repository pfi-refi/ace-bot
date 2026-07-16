"""
Ace's brain for the portal — Anthropic call + action-tag loop.

Ported from ace-bot/bot.py `_process_text`. Same architecture: live data +
memory are injected into the system prompt, Claude replies with inline action
tags ([CREATE_EVENT:], [ADD_TASK:], …), and the tags are parsed out and executed
after the response. The ONLY adaptation is streaming: tokens are streamed to the
browser with tag text filtered out live, then tags are executed once the full
response is in hand (tags need the complete text before they can run).

Model + token budget are env-configurable (ACE_MODEL / ACE_MAX_TOKENS) and default
to the exact values the live Telegram bot uses, so both interfaces are one Ace.
"""

import logging
import os
import re
from datetime import datetime

import anthropic

from .calendar_api import (
    create_calendar_event,
    delete_calendar_event,
    get_calendar_events,
    get_calendar_events_range,
    get_tomorrow_events,
)
from .google_client import EASTERN
from .memory import (
    merge_memories,
    read_conversation_history,
    read_memory,
    write_conversation_history,
    write_memory,
)
from .system_prompt import SYSTEM_PROMPT
from .tasks_api import (
    add_task,
    complete_task,
    draft_email,
    get_gmail_summary,
    get_recent_read_emails,
    get_tasks,
    search_drive,
    send_email,
)

logger = logging.getLogger("ace_portal.chat")

ACE_MODEL = os.environ.get("ACE_MODEL", "claude-opus-4-8")
ACE_MAX_TOKENS = int(os.environ.get("ACE_MAX_TOKENS", "900"))

# Ace self-awareness note (condensed from bot.py get_ace_self_description) ────────
_SELF_DESCRIPTION = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACE SYSTEM STATUS — running in the PFI Command Center portal + Telegram (shared brain)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
All conversation history and memory files live on Google Drive — NEVER erased by code updates. Brady's deals, agents, and stored context are always preserved.
Auto-briefs are PAUSED. Brady triggers his brief manually. Do NOT tell him briefs will arrive automatically.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

_KNOWN_TAGS = (
    "MEMORY", "ADD_TASK", "COMPLETE_TASK", "SEND_EMAIL",
    "DRAFT_EMAIL", "SEARCH_DRIVE", "CREATE_EVENT", "DELETE_EVENT",
)


class TagStripper:
    """Incrementally removes [ACTION_TAG: ...] segments from a token stream so the
    browser never flashes raw tags. Non-tag bracket text is passed through."""

    def __init__(self):
        self._hold = ""
        self._in_bracket = False

    def feed(self, chunk: str) -> str:
        out = []
        for ch in chunk:
            if not self._in_bracket:
                if ch == "[":
                    self._in_bracket = True
                    self._hold = "["
                else:
                    out.append(ch)
            else:
                self._hold += ch
                if ch == "]":
                    inner = self._hold[1:-1]
                    key = inner.split(":", 1)[0].strip()
                    if key not in _KNOWN_TAGS:
                        out.append(self._hold)  # ordinary bracketed text — keep it
                    self._hold = ""
                    self._in_bracket = False
        return "".join(out)

    def flush(self) -> str:
        """Emit any trailing text if the stream ended mid-bracket and it does not
        look like the start of a known action tag."""
        if not self._in_bracket:
            return ""
        partial_key = self._hold[1:].split(":", 1)[0].strip()
        leftover = ""
        if partial_key and not any(
            k.startswith(partial_key) or partial_key.startswith(k) for k in _KNOWN_TAGS
        ):
            leftover = self._hold
        self._hold = ""
        self._in_bracket = False
        return leftover


def build_system_context(user_text: str) -> str:
    """Assemble the full system prompt with live data + memory (ported from bot.py)."""
    memories = read_memory()
    tasks_data = get_tasks()
    calendar_data = get_calendar_events()
    tomorrow_events = get_tomorrow_events()
    email_data = get_gmail_summary()
    recent_email_data = get_recent_read_emails()

    msg_lower = user_text.lower()
    if any(p in msg_lower for p in ['next week', 'this week', 'next 7', '7 days', 'week ahead', 'upcoming', 'next 10', '10 days']):
        calendar_range = get_calendar_events_range(days=10)
    elif any(p in msg_lower for p in ['next month', '30 days', 'this month', 'month ahead']):
        calendar_range = get_calendar_events_range(days=30)
    else:
        calendar_range = ""

    memory_context = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_context = f"\n\n📋 WHAT ACE KNOWS ABOUT BRADY (from memory):\n{memory_str}"

    now_et = datetime.now(EASTERN)
    live_data = (
        f"\n\n📊 LIVE DATA (auto-fetched right now — {now_et.strftime('%A, %B %-d, %Y %-I:%M %p ET')}):\n"
        f"📅 TODAY'S CALENDAR:\n{calendar_data}\n\n"
        f"📅 TOMORROW'S SCHEDULE:\n{tomorrow_events}\n\n"
        + (f"📆 UPCOMING CALENDAR RANGE:\n{calendar_range}\n\n" if calendar_range else "")
        + f"✅ OPEN TASKS:\n{tasks_data or 'No open tasks.'}\n\n"
        f"📧 UNREAD EMAILS:\n{email_data}\n\n"
        f"📨 RECENT READ EMAILS (last 48hrs):\n{recent_email_data}"
    )

    system_with_context = (
        SYSTEM_PROMPT
        + _SELF_DESCRIPTION
        + live_data
        + memory_context
        + "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 CURRENT TIME: {now_et.strftime('%A, %B %-d, %Y — %-I:%M %p ET')} (live, injected every message — always accurate. Use this to give Brady time-aware responses: flag upcoming events, note time of day, calculate how long until next appointment.)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 EXECUTION MANDATE — OVERRIDES EVERYTHING INCLUDING CONVERSATION HISTORY:\n"
        "All integrations are FULLY OPERATIONAL. Past errors are resolved. Execute on the FIRST ask. Every time.\n\n"
        "TRIGGER LANGUAGE → ACTION TAG (fire in the SAME response, no delay, no confirmation first):\n"
        "• 'book', 'schedule', 'set up', 'add to calendar', 'block off', 'put on my calendar', 'set a meeting', 'book a meeting' → [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration | description]\n"
        "• 'delete', 'remove', 'cancel', 'clear', 'get rid of', 'take off my calendar', 'remove from calendar' → [DELETE_EVENT: title | YYYY-MM-DD]\n"
        "• 'add a task', 'remind me', 'don\\'t let me forget', 'note that', 'put that on my list' → [ADD_TASK: title | list]\n"
        "• 'done', 'handled', 'crossed that off', 'took care of', 'finished', 'got it done', 'that\\'s done' → [COMPLETE_TASK: partial title]\n\n"
        "ZERO TOLERANCE RULES:\n"
        "1. Never describe an action in text without also outputting the tag in the SAME response — text + no tag = failure.\n"
        "2. If a tag fired and returned ⚠️ — immediately generate the corrected tag without waiting for Brady to ask again.\n"
        "3. Never say 'shall I go ahead?', 'want me to do that?', or 'should I schedule it?' — EXECUTE FIRST, confirm after.\n"
        "4. One ask = one execution. No exceptions. No hesitation.\n"
        "📅 CALENDAR DATA: Pulled LIVE on every message. Never tell Brady it's a snapshot or frozen. Always current.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTION TAG REFERENCE (use these in your response when appropriate):\n"
        "• [ADD_TASK: task title | list name] — adds task immediately (list optional, defaults to Brain Dump)\n"
        "• [COMPLETE_TASK: partial title] — marks task done via fuzzy match\n"
        "• [SEND_EMAIL: to@email.com | Subject | Body] — sends email immediately\n"
        "• [DRAFT_EMAIL: to@email.com | Subject | Body] — creates Gmail draft\n"
        "• [SEARCH_DRIVE: query] — searches Drive, result appended to your reply\n"
        "• [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration_mins | description] — creates calendar event (time + description optional)\n"
        "• [DELETE_EVENT: title | YYYY-MM-DD] — deletes matching calendar event\n"
        "• [MEMORY: brief fact] — saves to long-term memory for future sessions\n"
        "Include 0–3 [MEMORY:] tags max. Skip tagging trivial chat. "
        "Tags are invisible to Brady — he only sees your clean response plus action confirmations."
    )
    return system_with_context, memories


def _strip_tags(response: str) -> str:
    """Canonical tag removal (ported from bot.py) — used for the saved/clean text."""
    clean = re.sub(r'\[MEMORY:[^\]]+\]', '', response)
    clean = re.sub(r'\[ADD_TASK:[^\]]+\]', '', clean)
    clean = re.sub(r'\[COMPLETE_TASK:[^\]]+\]', '', clean)
    clean = re.sub(r'\[SEND_EMAIL:[^\]]+\]', '', clean, flags=re.DOTALL)
    clean = re.sub(r'\[DRAFT_EMAIL:[^\]]+\]', '', clean, flags=re.DOTALL)
    clean = re.sub(r'\[SEARCH_DRIVE:[^\]]+\]', '', clean)
    clean = re.sub(r'\[CREATE_EVENT:[^\]]+\]', '', clean)
    clean = re.sub(r'\[DELETE_EVENT:[^\]]+\]', '', clean)
    return clean.strip()


def execute_tags(response: str, memories: list) -> list:
    """Parse and execute all action tags. Returns list of confirmation strings.

    Ported from bot.py — same behavior, same confirmations. Errors are returned as
    confirmation strings (never raised) so the WS loop stays alive.
    """
    memory_tags = re.findall(r'\[MEMORY:\s*([^\]]+)\]', response)
    add_task_tags = re.findall(r'\[ADD_TASK:\s*([^\]]+)\]', response)
    complete_tags = re.findall(r'\[COMPLETE_TASK:\s*([^\]]+)\]', response)
    send_email_tags = re.findall(r'\[SEND_EMAIL:\s*([^\]]+)\]', response, re.DOTALL)
    draft_email_tags = re.findall(r'\[DRAFT_EMAIL:\s*([^\]]+)\]', response, re.DOTALL)
    drive_tags = re.findall(r'\[SEARCH_DRIVE:\s*([^\]]+)\]', response)
    create_event_tags = re.findall(r'\[CREATE_EVENT:\s*([^\]]+)\]', response)
    delete_event_tags = re.findall(r'\[DELETE_EVENT:\s*([^\]]+)\]', response)

    confirmations = []

    if memory_tags:
        try:
            merged = merge_memories(memory_tags, memories)
            if write_memory(merged):
                logger.info("Stored %d new memory item(s).", len(memory_tags))
        except Exception as e:
            logger.error("Memory store error: %s", e)

    for tag in add_task_tags:
        parts = [p.strip() for p in tag.split("|", 1)]
        title = parts[0]
        list_name = parts[1] if len(parts) > 1 else "Brain Dump"
        success, actual_list, was_dup = add_task(title, list_name)
        if success:
            confirmations.append(
                (f"ℹ️ Already in {actual_list}: {title}") if was_dup
                else (f"✅ Added to {actual_list}: {title}")
            )
        else:
            confirmations.append(f"⚠️ Couldn't add task: {title}")

    for tag in complete_tags:
        completed = complete_task(tag.strip())
        confirmations.append(
            f"✅ Completed: {completed}" if completed
            else f"⚠️ Task not found to complete: {tag.strip()}"
        )

    for tag in send_email_tags:
        parts = [p.strip() for p in tag.split("|", 2)]
        if len(parts) >= 3:
            to_addr, subject, body = parts[0], parts[1], parts[2]
            confirmations.append(
                f"📤 Email sent to {to_addr} — {subject}" if send_email(to_addr, subject, body)
                else f"⚠️ Email failed: {to_addr}"
            )
        else:
            confirmations.append(f"⚠️ Malformed SEND_EMAIL tag: {tag[:40]}")

    for tag in draft_email_tags:
        parts = [p.strip() for p in tag.split("|", 2)]
        if len(parts) >= 3:
            to_addr, subject, body = parts[0], parts[1], parts[2]
            confirmations.append(
                f"📝 Draft saved for {to_addr} — {subject}" if draft_email(to_addr, subject, body)
                else f"⚠️ Draft failed: {to_addr}"
            )
        else:
            confirmations.append(f"⚠️ Malformed DRAFT_EMAIL tag: {tag[:40]}")

    for tag in drive_tags:
        results = search_drive(tag.strip())
        confirmations.append(f"🔍 Drive — '{tag.strip()}':\n{results}")

    for tag in create_event_tags:
        parts = [p.strip() for p in tag.split("|")]
        if len(parts) >= 2:
            title = parts[0]
            date_s = parts[1]
            time_s = parts[2] if len(parts) > 2 else None
            dur = int(parts[3]) if len(parts) > 3 and parts[3].strip().isdigit() else 60
            desc = parts[4] if len(parts) > 4 else ""
            success, msg = create_calendar_event(title, date_s, time_s, dur, desc)
            if success:
                time_label = f" at {time_s}" if time_s else ""
                confirmations.append(f"📅 Added to calendar: {title} on {date_s}{time_label}")
            else:
                confirmations.append(f"❌ Calendar booking failed: {msg}")
        else:
            confirmations.append(f"⚠️ Malformed CREATE_EVENT tag: {tag[:50]}")

    for tag in delete_event_tags:
        parts = [p.strip() for p in tag.split("|")]
        if len(parts) >= 2:
            title, date_s = parts[0], parts[1]
            success, msg = delete_calendar_event(title, date_s)
            if success:
                confirmations.append(f"🗑️ Removed from calendar: {msg} on {date_s}")
            else:
                confirmations.append(f"❌ Calendar delete failed: {msg}")
        else:
            confirmations.append(f"⚠️ Malformed DELETE_EVENT tag: {tag[:50]}")

    return confirmations


async def stream_reply(user_text: str, emit):
    """Run one Ace turn with streaming.

    `emit` is an async callback: await emit(event_type, payload). Event types:
      "delta"        — {"text": <clean token chunk>}   (live, tag-free)
      "final"        — {"text": <full clean response>}
      "confirmation" — {"text": <one action confirmation>}
      "error"        — {"text": <message>}
    """
    try:
        system_with_context, memories = build_system_context(user_text)
        conversation_history = read_conversation_history()
        messages = list(conversation_history)
        messages.append({"role": "user", "content": user_text})

        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        stripper = TagStripper()
        raw_parts = []

        async with client.messages.stream(
            model=ACE_MODEL,
            max_tokens=ACE_MAX_TOKENS,
            system=system_with_context,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                raw_parts.append(text)
                visible = stripper.feed(text)
                if visible:
                    await emit("delta", {"text": visible})
        tail = stripper.flush()
        if tail:
            await emit("delta", {"text": tail})

        raw_response = "".join(raw_parts)
        clean_response = _strip_tags(raw_response)
        await emit("final", {"text": clean_response})

        # Execute action tags AFTER the full response is in hand.
        confirmations = execute_tags(raw_response, memories)
        for c in confirmations:
            await emit("confirmation", {"text": c})

        # Persist shared conversation history (portal + Telegram stay in sync).
        updated_history = list(conversation_history)
        updated_history.append({"role": "user", "content": user_text})
        updated_history.append({"role": "assistant", "content": clean_response})
        write_conversation_history(updated_history)

        return clean_response
    except Exception as e:
        logger.error("stream_reply error: %s", e)
        await emit("error", {"text": f"⚠️ Error: {e}"})
        return ""
