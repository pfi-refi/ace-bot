"""
TEMPORARY recovery tool — inspect Google Drive revision history.

WHY THIS EXISTS: the portal trimmed the shared ace_conversation.json to 80
messages while the Telegram bot keeps 160, so every portal chat turn silently
deleted the oldest half of the bot's history. Fixed in write_conversation_history,
but history already lost needs recovering from Drive's revision history.

That recovery is possible at all only because both services write with
files().update(fileId=...) rather than delete-and-recreate, so Drive retains
prior revisions of the file.

READ-ONLY BY CONSTRUCTION. This module lists and downloads revisions. It has no
write, update, or delete path — restoring is a deliberate separate act, taken
only once we can see what is actually there and what it would cost.

Delete this module once recovery is done or written off.
"""

import json
import logging

from googleapiclient.discovery import build

from .google_client import CONVERSATION_FILE_NAME, get_google_creds

logger = logging.getLogger("ace_portal.revisions")


def _find_file_id(service, name: str):
    results = service.files().list(
        q=f"name='{name}' and trashed=false",
        spaces="drive",
        fields="files(id, name)",
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def list_revisions(name: str = CONVERSATION_FILE_NAME) -> dict:
    """List every retained revision of the file, newest last.

    Reports each revision's message count so we can see exactly where the trim
    bug bit and which revision is the richest candidate for recovery.
    """
    out = {"ok": False, "file": name, "file_id": None, "revisions": [], "error": ""}
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        file_id = _find_file_id(service, name)
        if not file_id:
            out["error"] = "file not found on Drive"
            return out
        out["file_id"] = file_id

        revs = service.revisions().list(
            fileId=file_id,
            fields="revisions(id, modifiedTime, size, keepForever)",
        ).execute().get("revisions", [])

        for r in revs:
            entry = {
                "id": r.get("id"),
                "modifiedTime": r.get("modifiedTime"),
                "size": r.get("size"),
                "keepForever": r.get("keepForever", False),
                "messages": None,
            }
            # Count messages per revision — the whole point is finding the one
            # with the most history still intact.
            try:
                raw = service.revisions().get_media(
                    fileId=file_id, revisionId=r.get("id")
                ).execute()
                entry["messages"] = len(json.loads(raw).get("messages", []))
            except Exception as e:
                entry["messages"] = f"unreadable: {e}"
            out["revisions"].append(entry)

        out["ok"] = True
        return out
    except Exception as e:
        logger.error("Revision list error: %s", e)
        out["error"] = str(e)
        return out


def get_revision(revision_id: str, name: str = CONVERSATION_FILE_NAME) -> dict:
    """Download one revision's contents. Read-only; nothing is written."""
    out = {"ok": False, "revision_id": revision_id, "messages": [], "error": ""}
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        file_id = _find_file_id(service, name)
        if not file_id:
            out["error"] = "file not found on Drive"
            return out
        raw = service.revisions().get_media(fileId=file_id, revisionId=revision_id).execute()
        out["messages"] = json.loads(raw).get("messages", [])
        out["ok"] = True
        return out
    except Exception as e:
        logger.error("Revision fetch error: %s", e)
        out["error"] = str(e)
        return out
