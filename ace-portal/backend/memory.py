"""
Ace memory + conversation history on Google Drive.

Ported directly from ace-bot/bot.py. ace_memory.json is the SOURCE OF TRUTH for
all business context — the portal never hardcodes PFI numbers, it reads them here.

SECURITY: read/write ONLY. These functions never delete or trash Drive files and
never overwrite the {"memories": [...]} / {"messages": [...]} envelope structure.
"""

import io
import json
import logging
import os

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .google_client import (
    CONVERSATION_FILE_NAME,
    MEMORY_FILE_NAME,
    get_google_creds,
)

logger = logging.getLogger("ace_portal.memory")


# ── Memory (Google Drive) ───────────────────────────────────────────────────────
def read_memory() -> list:
    """Read Ace's memory list from Google Drive. Returns [] if unavailable."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        results = service.files().list(
            q=f"name='{MEMORY_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id, name)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return []
        file_id = files[0]["id"]
        raw = service.files().get_media(fileId=file_id).execute()
        data = json.loads(raw)
        return data.get("memories", [])
    except Exception as e:
        err = str(e)
        if "403" in err or "insufficient" in err.lower() or "scope" in err.lower():
            logger.warning("Drive scope not yet active — memory inactive until re-auth.")
        else:
            logger.error("Memory read error: %s", e)
        return []


def write_memory(memories: list) -> bool:
    """Write memory list to Google Drive (create or update ace_memory.json).

    Update-in-place only — the existing file id is reused; the file is never
    deleted and re-created, so revision history on Drive is preserved.
    """
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        payload = json.dumps({"memories": memories}, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        results = service.files().list(
            q=f"name='{MEMORY_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id)",
        ).execute()
        files = results.get("files", [])
        if files:
            service.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": MEMORY_FILE_NAME},
                media_body=media,
                fields="id",
            ).execute()
        logger.info("Memory written (%d items).", len(memories))
        return True
    except Exception as e:
        err = str(e)
        if "403" in err or "insufficient" in err.lower() or "scope" in err.lower():
            logger.warning("Drive scope not yet active — cannot write memory.")
        else:
            logger.error("Memory write error: %s", e)
        return False


def merge_memories(new_items: list, existing: list) -> list:
    """Ask Claude (Haiku) to merge new facts into existing memory, deduplicating.

    Ported from bot.py `_merge_memories`. Kept for portal use per spec.
    """
    if not new_items:
        return existing
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    existing_str = "\n".join(f"- {m}" for m in existing) or "(none yet)"
    new_str = "\n".join(f"- {m}" for m in new_items)
    prompt = (
        "You maintain Ace's operational memory about Brady McGraw (PFI Marketing Director).\n\n"
        f"EXISTING MEMORY:\n{existing_str}\n\n"
        f"NEW ITEMS TO ADD:\n{new_str}\n\n"
        "Merge the new items into the existing memory. Rules:\n"
        "1. Remove exact or near-duplicate facts\n"
        "2. If new info contradicts old, keep the newer version\n"
        "3. Keep entries concise (one fact per line, ~15 words max)\n"
        "4. Max 60 total entries — drop least relevant if over\n"
        "5. Return ONLY the final merged list, one item per line, no bullets or numbering"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    merged = [line.strip() for line in response.content[0].text.strip().split("\n") if line.strip()]
    return merged


# ── Conversation History (Google Drive) — shared with Telegram bot ───────────────
def read_conversation_history() -> list:
    """Load last 40 exchanges from ace_conversation.json on Drive. [] if unavailable."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        results = service.files().list(
            q=f"name='{CONVERSATION_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id, name)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return []
        file_id = files[0]["id"]
        raw = service.files().get_media(fileId=file_id).execute()
        data = json.loads(raw)
        return data.get("messages", [])
    except Exception as e:
        logger.warning("Conversation history read error: %s", e)
        return []


def write_conversation_history(messages: list) -> bool:
    """Save conversation history to ace_conversation.json on Drive (max 80 msgs).

    Update-in-place only; never deletes the file. Trims to the last 80 messages
    (40 exchanges) to match the Telegram bot so both interfaces stay in sync.
    """
    try:
        if len(messages) > 80:
            messages = messages[-80:]
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        payload = json.dumps({"messages": messages}, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        results = service.files().list(
            q=f"name='{CONVERSATION_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id)",
        ).execute()
        files = results.get("files", [])
        if files:
            service.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": CONVERSATION_FILE_NAME},
                media_body=media,
                fields="id",
            ).execute()
        logger.info("Conversation history written (%d messages).", len(messages))
        return True
    except Exception as e:
        logger.warning("Conversation history write error: %s", e)
        return False
