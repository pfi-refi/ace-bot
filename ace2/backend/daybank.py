"""
Ace 2.0's DATA BANK — Brady's auto-captured second brain, on Google Drive.

As the day unfolds, Ace captures the things worth not forgetting — commitments,
follow-ups, "don't forget X," loose todos — into this store HIMSELF (via the
capture_item tool). It's Brady's ambient to-do surface: he reviews it here
instead of living in Google Tasks.

Like history.py, this file is Ace 2.0's ALONE — never the shared
ace_conversation.json (whose absence of a writer protects the Telegram bot,
brain.py:4-20). So it can carry any schema we want.

Single running file (not monthly): it's a persistent second brain, so open items
carry forward across days; done items are retained but age out of the default
view. Read-modify-write of the whole file, same as memory/history — fine for a
single user; last-writer-wins on the rare concurrent mutation.

Shape: {"items": [{"id","ts","kind","text","status","tags":[],"due":null|str,"done_ts":null|str}]}
  kind   ∈ note | todo | commitment | followup
  status ∈ open | done
"""

import io
import json
import logging
import uuid
from datetime import datetime

import pytz
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .integrations.google_client import get_google_creds

logger = logging.getLogger("ace2.daybank")

EASTERN = pytz.timezone("America/New_York")
FILE_NAME = "ace2_daybank.json"
KINDS = ("note", "todo", "commitment", "followup")


def _drive():
    return build("drive", "v3", credentials=get_google_creds())


def _find(service, name: str):
    files = service.files().list(
        q=f"name='{name}' and trashed=false", spaces="drive", fields="files(id)"
    ).execute().get("files", [])
    return files[0]["id"] if files else None


def _load(service, fid):
    if not fid:
        return []
    try:
        raw = service.files().get_media(fileId=fid).execute()
        return json.loads(raw).get("items", [])
    except Exception:
        return []


def _save(service, fid, items: list):
    payload = json.dumps({"items": items}, indent=2).encode()
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
    if fid:
        service.files().update(fileId=fid, media_body=media).execute()
    else:
        service.files().create(body={"name": FILE_NAME}, media_body=media, fields="id").execute()


def read_items(active_only: bool = True) -> list:
    """Read the data bank. active_only → open items (any day) + items touched today.

    That's the default HUD view: carried-forward open work plus what got captured or
    completed today. Newest first. [] on any failure — never raises.
    """
    try:
        service = _drive()
        items = _load(service, _find(service, FILE_NAME))
        if active_only:
            today = datetime.now(EASTERN).strftime("%Y-%m-%d")
            items = [
                it for it in items
                if it.get("status") == "open"
                or (it.get("ts", "")[:10] == today)
                or (it.get("done_ts", "") or "")[:10] == today
            ]
        items.sort(key=lambda it: it.get("ts", ""), reverse=True)
        return items
    except Exception as e:
        logger.warning("daybank read failed: %s", e)
        return []


def add_item(kind: str, text: str, due: str = None, tags: list = None) -> tuple:
    """Capture one item. Returns (ok, item_or_error). Best-effort; never raises."""
    text = (text or "").strip()
    if not text:
        return False, "empty text"
    kind = (kind or "note").strip().lower()
    if kind not in KINDS:
        kind = "note"
    try:
        service = _drive()
        fid = _find(service, FILE_NAME)
        items = _load(service, fid)
        item = {
            "id": uuid.uuid4().hex[:8],
            "ts": datetime.now(EASTERN).isoformat(),
            "kind": kind,
            "text": text,
            "status": "open",
            "tags": tags or [],
            "due": (due or None),
            "done_ts": None,
        }
        items.append(item)
        _save(service, fid, items)
        return True, item
    except Exception as e:
        logger.error("daybank add failed: %s", e)
        return False, str(e)


def update_item(item_id: str, status: str = None, text: str = None) -> tuple:
    """Complete/reopen or edit an item by id. Returns (ok, message). Never raises."""
    item_id = (item_id or "").strip()
    if not item_id:
        return False, "no id"
    try:
        service = _drive()
        fid = _find(service, FILE_NAME)
        items = _load(service, fid)
        for it in items:
            if it.get("id") == item_id:
                if status in ("open", "done"):
                    it["status"] = status
                    it["done_ts"] = datetime.now(EASTERN).isoformat() if status == "done" else None
                if text and text.strip():
                    it["text"] = text.strip()
                _save(service, fid, items)
                return True, it.get("text", item_id)
        return False, f"no item {item_id}"
    except Exception as e:
        logger.error("daybank update failed: %s", e)
        return False, str(e)
