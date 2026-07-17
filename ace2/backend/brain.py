"""
Ace's shared brain — Google Drive.

THE RULE THAT MAKES ACE 2.0 SAFE:

    ace_memory.json        read + write   (shared — this is what makes 2.0 the
                                           SAME Ace as the Telegram bot)
    ace_conversation.json  READ-ONLY      (continuity only — NEVER written)

The Telegram bot is Brady's daily driver and it feeds ace_conversation.json
records straight to the Anthropic API with no sanitization (bot.py:1918, :2008,
:2601 -> :2625). So any extra key on those records — a timestamp, a source
marker — makes the API reject the request and kills the bot. The two services
also both read-modify-write the whole file, which is a real last-writer-wins race.

Ace 2.0 sidesteps all of it by never writing that file. There is deliberately no
write_conversation_history() in this codebase: its ABSENCE is the safety
mechanism. Don't add one. Ace 2.0's own history is append-only and lives in
history.py, with timestamps and source markers it can define freely because
nothing else reads it.

Everything here is blocking Google I/O. Callers MUST wrap these in
asyncio.to_thread — the portal's habit of calling them straight from an async
handler stalls the whole event loop, which is merely slow for text and fatal for
realtime voice.
"""

import io
import json
import logging
import os

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .integrations.google_client import (
    CONVERSATION_FILE_NAME,
    MEMORY_FILE_NAME,
    get_google_creds,
)

logger = logging.getLogger("ace2.brain")

# Recovered pre-2026-07-15 history, rebuilt by unioning Drive revisions after a
# /clear or /reset wiped the live window. Read-only archive; superset of it.
RECOVERED_FILE_NAME = "ace_history_recovered.json"

MEMORY_CAP = 60


def google_ready() -> bool:
    """Are Google credentials actually configured?

    Needed because an empty result is ambiguous: the integrations swallow their
    own exceptions and return [], so "no tasks today" and "Drive is unreachable"
    look identical to the caller. get_google_creds() doesn't help either — with
    no GOOGLE_TOKEN_JSON it happily returns a Credentials object full of Nones
    rather than raising.

    Without this the status dots report every service green while nothing works,
    which is worse than no indicator at all.

    Caveat: this proves configuration, not reachability. A live Google outage
    still surfaces as empty data with a green dot; the integrations log it.
    """
    try:
        data = json.loads(os.environ.get("GOOGLE_TOKEN_JSON", "{}"))
    except Exception:
        return False
    return bool(data.get("refresh_token") and data.get("client_id"))


def _drive():
    return build("drive", "v3", credentials=get_google_creds())


def _find(service, name: str):
    files = service.files().list(
        q=f"name='{name}' and trashed=false", spaces="drive", fields="files(id)"
    ).execute().get("files", [])
    return files[0]["id"] if files else None


def _read_json(name: str) -> dict:
    try:
        service = _drive()
        fid = _find(service, name)
        if not fid:
            return {}
        return json.loads(service.files().get_media(fileId=fid).execute())
    except Exception as e:
        err = str(e)
        if "403" in err or "insufficient" in err.lower() or "scope" in err.lower():
            logger.warning("Drive scope inactive — %s unavailable.", name)
        else:
            logger.error("Drive read error (%s): %s", name, e)
        return {}


# ── Memory: read + write (shared with the Telegram bot) ─────────────────────────
def read_memory() -> list:
    return _read_json(MEMORY_FILE_NAME).get("memories", [])


def write_memory(memories: list) -> bool:
    """Update ace_memory.json in place. Never deletes; Drive keeps revisions.

    (That update-in-place habit is the only reason the /clear wipe was
    recoverable at all — keep it.)
    """
    try:
        service = _drive()
        payload = json.dumps({"memories": memories}, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        fid = _find(service, MEMORY_FILE_NAME)
        if fid:
            service.files().update(fileId=fid, media_body=media).execute()
        else:
            service.files().create(
                body={"name": MEMORY_FILE_NAME}, media_body=media, fields="id"
            ).execute()
        logger.info("Memory written (%d items).", len(memories))
        return True
    except Exception as e:
        logger.error("Memory write error: %s", e)
        return False


def merge_memories(new_items: list, existing: list) -> list:
    """Merge new facts into memory via Haiku, with a local fallback.

    The portal has NO fallback here — an API hiccup silently loses the memory.
    This mirrors the bot's safer behaviour (bot.py:520-557) and keeps the bot's
    rule about converting relative dates to absolute, which the portal dropped,
    so memories don't rot into "tomorrow" and "Friday".
    """
    if not new_items:
        return existing

    def _local() -> list:
        merged = list(existing)
        for item in new_items:
            if not any(item.strip().lower() == e.strip().lower() for e in merged):
                merged.append(item)
        return merged[-MEMORY_CAP:]

    try:
        import anthropic
        from datetime import datetime
        import pytz

        today = datetime.now(pytz.timezone("America/New_York")).strftime("%A, %B %d, %Y")
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        prompt = (
            "You maintain Ace's operational memory about Brady McGraw (PFI Marketing Director).\n\n"
            f"EXISTING MEMORY:\n" + ("\n".join(f"- {m}" for m in existing) or "(none yet)") + "\n\n"
            f"NEW ITEMS TO ADD:\n" + "\n".join(f"- {m}" for m in new_items) + "\n\n"
            "Merge the new items into the existing memory. Rules:\n"
            "1. Remove exact or near-duplicate facts\n"
            "2. If new info contradicts old, keep the newer version\n"
            "3. Keep entries concise (one fact per line, ~15 words max)\n"
            f"4. Max {MEMORY_CAP} total entries — drop least relevant if over\n"
            f"5. Convert relative dates to absolute using today's date ({today}) — "
            "never store 'tomorrow' or 'next Friday'\n"
            "6. Return ONLY the final merged list, one item per line, no bullets or numbering"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        merged = [ln.strip() for ln in resp.content[0].text.strip().split("\n") if ln.strip()]
        if not merged:
            logger.warning("merge_memories: empty merge — using local fallback")
            return _local()
        return merged
    except Exception as e:
        logger.error("merge_memories API error (%s) — using local fallback", e)
        return _local()


# ── Conversation: READ-ONLY. There is no writer here, on purpose. ───────────────
def read_shared_conversation() -> list:
    """Read the Telegram bot's rolling window for continuity.

    Returns [{"role","content"}]. Ace 2.0 reads this so it remembers what Brady
    told Ace on Telegram — and never writes it back, so the bot is untouchable.
    """
    return _read_json(CONVERSATION_FILE_NAME).get("messages", [])


def read_recovered_history() -> list:
    """Read the recovered pre-wipe archive. Read-only."""
    return _read_json(RECOVERED_FILE_NAME).get("messages", [])


def sanitize_for_api(messages: list) -> list:
    """Strip records to {role, content} AND make them API-legal.

    Defence in depth: shared files are written by another service and archives
    carry extra metadata. Beyond stripping keys, the Anthropic API has two hard
    rules that the shared Telegram window can violate depending on its exact
    state at read time — and would 400 the whole turn:
      • the first message must be role "user"  → drop leading assistant turns
      • roles must alternate                     → collapse consecutive same-role
    Enforcing both here means every caller feeds the API a legal transcript.
    """
    cleaned = []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content})

    # Drop any leading assistant messages so the list begins with a user turn.
    while cleaned and cleaned[0]["role"] == "assistant":
        cleaned.pop(0)

    # Collapse consecutive same-role messages (merge their text).
    out = []
    for m in cleaned:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append(dict(m))
    return out
