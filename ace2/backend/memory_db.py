"""
Ace 2.0's memory database v1 — TOTAL RECALL over everything Ace has ever seen.

One entry point: recall(query) — keyword search, scored and recency-tied, across
every store Ace owns or reads:
  • ALL ace2 monthly history files (every turn ever exchanged with 2.0)
  • the shared Telegram conversation window (read-only continuity with the bot)
  • the recovered pre-wipe archive (ace_history_recovered.json, 496 msgs)
  • durable memory facts (ace_memory.json)
  • the data bank (ace2_daybank.json — deals/commitments/notes)

This is deliberately infrastructure-free (no vector DB, no new service): keyword
scoring over JSON stores already on Drive, with a short in-process cache so
repeat recalls inside a conversation are instant. If/when volume outgrows this,
the swap is mem0/pgvector behind the same recall() signature.

Reads are best-effort per source — one store failing never kills the search.
"""

import json
import logging
import re
import time
from datetime import datetime

import pytz
from googleapiclient.discovery import build

from . import brain, daybank
from .integrations.google_client import get_google_creds

logger = logging.getLogger("ace2.memory_db")

EASTERN = pytz.timezone("America/New_York")

# Corpus cache: fetching every store costs several Drive reads; within a session
# Brady often recalls repeatedly ("what did we say about X… and about Y?").
_CACHE_TTL = 300  # seconds
_cache = {"ts": 0.0, "corpus": None}

_WORD = re.compile(r"[a-z0-9$][a-z0-9$'/.-]*")

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with",
    "about", "what", "when", "did", "do", "does", "we", "i", "you", "he", "she",
    "it", "that", "this", "was", "were", "is", "are", "be", "me", "my", "our",
    "his", "her", "say", "said", "tell", "told", "talk", "talked", "remember",
}


def _tokens(text: str) -> list:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def _read_all_history() -> list:
    """Every ace2 monthly history file, oldest-first. [] on failure."""
    try:
        service = build("drive", "v3", credentials=get_google_creds())
        files = service.files().list(
            q="name contains 'ace2_history_' and trashed=false",
            spaces="drive", fields="files(id, name)", orderBy="name",
        ).execute().get("files", [])
        out = []
        for f in files:
            try:
                raw = service.files().get_media(fileId=f["id"]).execute()
                out.extend(json.loads(raw).get("entries", []))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("memory_db: history read failed: %s", e)
        return []


def _build_corpus() -> list:
    """Normalize every store into [{ts, source, role, text}]. Best-effort per source."""
    corpus = []

    for e in _read_all_history():
        corpus.append({
            "ts": e.get("ts", ""), "source": "ace2",
            "role": e.get("role", ""), "text": e.get("content", ""),
        })

    try:
        for m in brain.read_shared_conversation():
            corpus.append({
                "ts": m.get("ts", ""), "source": "telegram",
                "role": m.get("role", ""), "text": m.get("content", "") if isinstance(m.get("content"), str) else "",
            })
    except Exception as e:
        logger.warning("memory_db: shared conv read failed: %s", e)

    try:
        for m in brain.read_recovered_history():
            corpus.append({
                "ts": m.get("ts", ""), "source": "archive",
                "role": m.get("role", ""), "text": m.get("content", "") if isinstance(m.get("content"), str) else "",
            })
    except Exception as e:
        logger.warning("memory_db: recovered read failed: %s", e)

    try:
        for fact in brain.read_memory():
            corpus.append({"ts": "", "source": "memory", "role": "fact",
                           "text": fact if isinstance(fact, str) else json.dumps(fact)})
    except Exception as e:
        logger.warning("memory_db: memory read failed: %s", e)

    try:
        for it in daybank.read_items(active_only=False):
            corpus.append({
                "ts": it.get("ts", ""), "source": "databank",
                "role": it.get("kind", "note"),
                "text": f"[{it.get('status','open')}] {it.get('text','')}"
                        + (f" (due {it['due']})" if it.get("due") else ""),
            })
    except Exception as e:
        logger.warning("memory_db: daybank read failed: %s", e)

    return corpus


def _corpus() -> list:
    now = time.time()
    if _cache["corpus"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["corpus"]
    corpus = _build_corpus()
    if corpus:  # don't cache a total failure
        _cache["corpus"] = corpus
        _cache["ts"] = now
    return corpus


def _score(entry_tokens: set, q_tokens: list) -> float:
    if not q_tokens:
        return 0.0
    hits = sum(1 for t in q_tokens if t in entry_tokens)
    return hits / len(q_tokens)


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(EASTERN).strftime("%b %-d, %-I:%M %p")
    except Exception:
        return ts or "undated"


def recall(query: str, max_results: int = 8) -> str:
    """Search everything Ace has ever seen. Returns matched snippets, newest first."""
    query = (query or "").strip()
    if not query:
        return "⚠️ recall needs a query."
    q_tokens = _tokens(query)
    if not q_tokens:
        return "⚠️ recall needs a few substantive words to search for."
    n = max(1, min(int(max_results), 20))

    corpus = _corpus()
    if not corpus:
        return "⚠️ Memory stores are unreachable right now."

    scored = []
    for e in corpus:
        text = e.get("text") or ""
        if not text.strip():
            continue
        s = _score(set(_tokens(text)), q_tokens)
        if s > 0:
            scored.append((s, e.get("ts", ""), e))
    if not scored:
        return f"Nothing in memory matches: {query}"

    # Best score first; within a score band, newest first.
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    lines = [f"MEMORY RECALL — top matches for \"{query}\":"]
    for s, ts, e in scored[:n]:
        who = {"user": "Brady", "assistant": "Ace", "fact": "FACT"}.get(e.get("role", ""), e.get("role", ""))
        src = e.get("source", "")
        snippet = re.sub(r"\s+", " ", e.get("text", "")).strip()
        if len(snippet) > 350:
            snippet = snippet[:350] + "…"
        lines.append(f"• [{_fmt_ts(ts)} · {src} · {who}] {snippet}")
    return "\n".join(lines)
