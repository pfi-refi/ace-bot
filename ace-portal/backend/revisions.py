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

import io
import json
import logging

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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



# Output target for recovery. Hardcoded, and deliberately NOT
# CONVERSATION_FILE_NAME: the live rolling window is read-only to this module,
# and recover_all() must be structurally incapable of writing to it.
RECOVERED_FILE_NAME = "ace_history_recovered.json"


def recover_all(name: str = CONVERSATION_FILE_NAME) -> dict:
    """Union every readable revision into one recovered history file on Drive.

    ace_conversation.json is a rolling window: once full, each new exchange
    pushes the oldest pair out. So each revision holds a DIFFERENT slice of the
    stream, and the union across revisions recovers far more than any single
    snapshot -- including messages destroyed by /clear and /reset, which write
    an empty list (bot.py cmd_clear_history / cmd_reset_history).

    Dedupes on (role, content), keeping first-appearance order and walking
    revisions oldest-first, so the result reads chronologically.

    Writes ONLY to RECOVERED_FILE_NAME, creating or updating in place. Never
    deletes. Never touches the source file.
    """
    out = {"ok": False, "scanned": 0, "readable": 0, "recovered": 0, "error": ""}
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        file_id = _find_file_id(service, name)
        if not file_id:
            out["error"] = "source file not found on Drive"
            return out

        revs = service.revisions().list(
            fileId=file_id, fields="revisions(id, modifiedTime)"
        ).execute().get("revisions", [])
        revs.sort(key=lambda r: r.get("modifiedTime") or "")
        out["scanned"] = len(revs)

        seen, merged = set(), []
        for r in revs:
            try:
                raw = service.revisions().get_media(
                    fileId=file_id, revisionId=r.get("id")
                ).execute()
                msgs = json.loads(raw).get("messages", [])
            except Exception:
                continue  # revisions Drive won't hand back (403) are simply skipped
            out["readable"] += 1
            for m in msgs:
                key = (m.get("role"), m.get("content"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append({"role": m.get("role"), "content": m.get("content")})

        payload = json.dumps({
            "recovered_from": name,
            "revisions_scanned": out["scanned"],
            "revisions_readable": out["readable"],
            "message_count": len(merged),
            "note": (
                "Union of all readable Drive revisions, deduped on (role, content), "
                "oldest-first. Superset of the live rolling window. Read-only archive "
                "-- not an API-shaped conversation file."
            ),
            "messages": merged,
        }, indent=2).encode()

        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        existing = service.files().list(
            q=f"name='{RECOVERED_FILE_NAME}' and trashed=false",
            spaces="drive", fields="files(id)",
        ).execute().get("files", [])
        if existing:
            service.files().update(fileId=existing[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": RECOVERED_FILE_NAME}, media_body=media, fields="id"
            ).execute()

        out["recovered"] = len(merged)
        out["ok"] = True
        logger.info("Recovered %d unique messages into %s", len(merged), RECOVERED_FILE_NAME)
        return out
    except Exception as e:
        logger.error("Recovery error: %s", e)
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
