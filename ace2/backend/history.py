"""
Ace 2.0's own conversation history — append-only, on Google Drive.

This is where 2.0 records its turns. Unlike the shared ace_conversation.json
(which 2.0 only reads — see brain.py), this file is 2.0's alone, so it can carry
timestamps and a source marker without any risk to the Telegram bot. Monthly
files keep any single file small; nothing is ever trimmed or overwritten in
place beyond appending.

Shape: {"entries": [{"ts": ISO8601, "source": "ace2", "role": "...", "content": "..."}]}

Every write is best-effort: a history failure must NEVER break a reply. chat.py
wraps append() so an exception here is logged and swallowed.
"""

import io
import json
import logging
from datetime import datetime

import pytz
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .integrations.google_client import get_google_creds

logger = logging.getLogger("ace2.history")

EASTERN = pytz.timezone("America/New_York")
SOURCE = "ace2"


def _month_file() -> str:
    return f"ace2_history_{datetime.now(EASTERN).strftime('%Y-%m')}.json"


def _find(service, name: str):
    files = service.files().list(
        q=f"name='{name}' and trashed=false", spaces="drive", fields="files(id)"
    ).execute().get("files", [])
    return files[0]["id"] if files else None


def append(role: str, content: str) -> bool:
    """Append one entry to this month's history file. Best-effort; never raises."""
    if not content or not content.strip():
        return False
    try:
        service = build("drive", "v3", credentials=get_google_creds())
        name = _month_file()
        fid = _find(service, name)
        entries = []
        if fid:
            try:
                raw = service.files().get_media(fileId=fid).execute()
                entries = json.loads(raw).get("entries", [])
            except Exception:
                entries = []
        entries.append({
            "ts": datetime.now(EASTERN).isoformat(),
            "source": SOURCE,
            "role": role,
            "content": content,
        })
        payload = json.dumps({"entries": entries}, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        if fid:
            service.files().update(fileId=fid, media_body=media).execute()
        else:
            service.files().create(body={"name": name}, media_body=media, fields="id").execute()
        return True
    except Exception as e:
        logger.warning("history append failed (%s) — reply unaffected", e)
        return False


def read_recent(months: int = 3) -> list:
    """Read the last N monthly history files, oldest-first. [] on any failure."""
    try:
        service = build("drive", "v3", credentials=get_google_creds())
        results = service.files().list(
            q="name contains 'ace2_history_' and trashed=false",
            spaces="drive", fields="files(id, name)", orderBy="name",
        ).execute().get("files", [])
        out = []
        for f in results[-months:]:
            try:
                raw = service.files().get_media(fileId=f["id"]).execute()
                out.extend(json.loads(raw).get("entries", []))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("history read failed: %s", e)
        return []
