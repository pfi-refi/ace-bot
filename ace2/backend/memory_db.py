"""
Ace 2.0's memory database v2 — TOTAL RECALL over everything Ace has ever seen.

One entry point: recall(query) — SEMANTIC/HYBRID search when the Postgres brain is
live (db.hybrid_search: english full-text + pg_trgm fuzzy, fused with Reciprocal
Rank Fusion), so Brady can ask by meaning — a paraphrase, a half-remembered name,
a typo — instead of guessing the exact words that were used at the time.

The original token-overlap keyword scan below is the fallback: it runs when
Postgres is dormant, and as a second pass when hybrid finds nothing, because it is
the only path that reaches stores Postgres doesn't hold —
every store Ace owns or reads:
  • ALL ace2 monthly history files (every turn ever exchanged with 2.0)
  • the shared Telegram conversation window (read-only continuity with the bot)
  • the recovered pre-wipe archive (ace_history_recovered.json, 496 msgs)
  • durable memory facts (ace_memory.json)
  • the data bank (ace2_daybank.json — deals/commitments/notes)

Still deliberately infrastructure-free — NO vector DB, no new service, no embedding
bill. The semantics come from Postgres itself (to_tsvector stemming + pg_trgm
similarity), which Ace already runs. If/when volume outgrows this, the swap is
pgvector behind the same recall() signature.

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
_HISTORY_SCAN = 5000   # how many recent Postgres turns to pull into the recall corpus

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

    # Ace's OWN conversation history. THE FIX: when the Postgres brain is live, history.append
    # writes turns to Postgres (backfilled from Drive at cutover) — but this search used to read
    # ONLY the frozen Drive files, so recall never saw a single Postgres turn ("he can't remember
    # what we talked about"). Read Postgres directly when it's on; fall back to Drive when dormant.
    # Additive either way: nothing is dropped, and the Drive-recovered archive is still merged below.
    try:
        from . import db
        if db.enabled():
            for t in db.recent_turns(_HISTORY_SCAN):
                corpus.append({
                    "ts": t.get("ts", ""), "source": "ace2",
                    "role": t.get("role", ""), "text": t.get("content", ""),
                })
        else:
            for e in _read_all_history():
                corpus.append({
                    "ts": e.get("ts", ""), "source": "ace2",
                    "role": e.get("role", ""), "text": e.get("content", ""),
                })
    except Exception as e:
        logger.warning("memory_db: history read failed: %s", e)

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
        from . import db
        if db.enabled():
            # Include ARCHIVED facts here (not in live context) so recall can still surface
            # retired facts as history — "at least he knows about it if asked" (Brady).
            for f in db.read_facts_full():
                tag = " [archived/history]" if (f.get("invalid_at") or f.get("tier") == "archived") else ""
                corpus.append({"ts": f.get("ts", ""), "source": "memory", "role": "fact",
                               "text": (f.get("text", "") or "") + tag})
        else:
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


def _render(query: str, rows: list) -> str:
    """rows: [{ts, source, who, text}] already ranked. One shared output format so the
    hybrid path and the legacy path look identical to Ace (and to any caller)."""
    lines = [f"MEMORY RECALL — top matches for \"{query}\":"]
    for r in rows:
        snippet = re.sub(r"\s+", " ", r.get("text", "") or "").strip()
        if len(snippet) > 350:
            snippet = snippet[:350] + "…"
        who = r.get("who") or ""
        lines.append(f"• [{_fmt_ts(r.get('ts', ''))} · {r.get('source','')} · {who}] {snippet}")
    return "\n".join(lines)


# Postgres source tag → the labels this function has always printed, so the output
# Ace reads is unchanged even though the retrieval underneath is completely new.
_PG_SOURCE = {"fact": "memory", "turn": "ace2", "item": "databank"}
_PG_WHO = {"user": "Brady", "assistant": "Ace"}


def _hybrid_recall(query: str, n: int) -> str:
    """Postgres hybrid search (full-text + trigram, RRF-fused). '' if it found nothing."""
    from . import db
    hits = db.hybrid_search(query, n)
    if not hits:
        return ""
    rows = []
    for h in hits:
        src = h.get("source", "")
        label = (h.get("label") or "").strip()
        if src == "fact":
            who = "FACT" + (f"/{label}" if label and label != "active" else "")
        elif src == "turn":
            who = _PG_WHO.get(label, label or "turn")
        else:
            who = label or "item"
        rows.append({"ts": h.get("ts", ""), "source": _PG_SOURCE.get(src, src),
                     "who": who, "text": h.get("text", "")})
    return _render(query, rows)


def _legacy_recall(query: str, n: int) -> str:
    """The original token-overlap scan over the merged JSON/Drive corpus. Still the
    only path that covers the Telegram window and the pre-wipe archive, so it stays."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return "⚠️ recall needs a few substantive words to search for."

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

    rows = [{
        "ts": ts, "source": e.get("source", ""),
        "who": {"user": "Brady", "assistant": "Ace", "fact": "FACT"}.get(
            e.get("role", ""), e.get("role", "")),
        "text": e.get("text", ""),
    } for s, ts, e in scored[:n]]
    return _render(query, rows)


def recall(query: str, max_results: int = 8) -> str:
    """Search everything Ace has ever seen — BY MEANING, not by literal keyword.

    Primary path is Postgres hybrid search (db.hybrid_search): english full-text
    (stemmed, stopword-free, so paraphrases hit) fused by Reciprocal Rank Fusion with
    pg_trgm fuzzy matching (typos, partial names). Falls back to the original
    token-overlap scan when Postgres is dormant OR when hybrid finds nothing — the
    legacy corpus is the only thing that covers the Telegram window and the pre-wipe
    archive, so a miss there is still worth a second look. Signature and return shape
    are unchanged: a formatted snippet block, best matches first.
    """
    query = (query or "").strip()
    if not query:
        return "⚠️ recall needs a query."
    n = max(1, min(int(max_results), 20))

    try:
        from . import db
        if db.enabled():
            out = _hybrid_recall(query, n)
            if out:
                return out
            logger.info("memory_db: hybrid found nothing for %r — trying legacy corpus", query)
    except Exception as e:
        logger.warning("memory_db: hybrid recall failed (%s) — falling back", e)

    return _legacy_recall(query, n)
