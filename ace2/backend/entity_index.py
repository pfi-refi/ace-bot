"""Incremental indexing — one source in, one alias pass, no model call, ever.

WHY THIS EXISTS (2026-09-21). `entity_migrate` is a one-shot: it reads the whole corpus
and builds the layer. The moment it finishes, Ace keeps writing — a fact here, a turn
there, a board row — and an index that is only ever rebuilt by a migration is an index
that is wrong by tomorrow morning. Rebuilding nightly was the alternative and it is the
thing this release exists to remove: a nightly rebuild is a paid model call whose output
has no stable ids, so yesterday's Sienna is not today's Sienna.

So `note(corpus, native_id)` is THE hook. db.py calls it at the end of `add_fact`,
`append_turn`, `add_item` and `update_item`, each wrapped in `try/except Exception: pass`
with a lazy import. Its contract, in order of importance:

  1. IT NEVER RAISES. A write to Ace's real memory must not fail because an index is
     unhappy. Every path returns None.
  2. IT IS A SILENT NO-OP WHEN THE TABLES DO NOT EXIST. Before the migration is applied —
     which is the state of every deployment until somebody runs it — this must cost one
     cheap probe and log nothing. It deliberately does NOT create tables: DDL from a hot
     write path is how two concurrent writes deadlock on a catalog lock.
  3. IT IS CHEAP. One row read, one source upsert, one in-memory alias scan, and a link
     per hit. No model call, no pairwise comparison, no full-corpus scan.

Everything about IDENTITY — normalization, the three-way resolve, first-name refusal,
tombstones — lives in `entities.py`. This module decides only WHAT text a source has,
WHICH class it belongs to, and then applies the policy.
"""

import logging
import queue
import re
import threading
import time

from . import db, entities

logger = logging.getLogger("ace2.entity_index")

INDEX_VERSION = 1

# Negative cache for the "layer not migrated yet" probe. A positive answer is permanent
# (tables are never dropped except by an explicit rollback, which restarts the process's
# luck anyway); a negative one expires, so applying the migration takes effect without a
# restart instead of leaving every later write silently unindexed.
_PROBE = {"ok": False, "checked": 0.0}
_PROBE_TTL = 30.0


def _layer_ready() -> bool:
    if _PROBE["ok"]:
        return True
    now = time.time()
    if now - _PROBE["checked"] < _PROBE_TTL:
        return False
    _PROBE["checked"] = now
    _PROBE["ok"] = entities.tables_exist()
    return _PROBE["ok"]


def reset_probe() -> None:
    """Forget the cached probe. Called by the migration after DDL, and by tests."""
    _PROBE["ok"] = False
    _PROBE["checked"] = 0.0


# ── Corpus readers ──────────────────────────────────────────────────────────────
# One record shape for every corpus, so the policy below is written once:
#   {corpus, native_id, text, occurred_at, role, kind, source_name}
#
# `summary` deliberately EXCLUDES kind='ace_profile': that single row is indexed as the
# `profile` corpus instead, so each of the 1814 summaries rows is indexed exactly once
# and the report's arithmetic reconciles against the manifest without double counting.
# ⚠ THE OUTPUT COLUMN IS ALIASED `native_id`, AND THE ORDER BY NAMES THE TABLE COLUMN.
# This is not cosmetic. Written as `SELECT id::text, … ORDER BY id`, Postgres resolves
# `id` to the OUTPUT column — the text cast — and sorts lexicographically: 1, 10, 100,
# 1000, 1001… while the cursor predicate `id > 1358` is numeric. Measured against the
# real corpus that combination silently skipped 958 of 2133 facts, 958 of 4154 turns and
# 957 of 1813 summaries, and reported a clean run. A pagination bug that DROPS RECORDS
# and says nothing is exactly the class of failure this whole layer exists to refuse.
_READ_SQL = {
    "fact": "SELECT id::text AS native_id, text, ts, source, tier, invalid_at FROM facts "
            "WHERE id > COALESCE(%s::bigint, -1) ORDER BY facts.id LIMIT %s",
    "turn": "SELECT id::text AS native_id, content, ts, role, source FROM turns "
            "WHERE id > COALESCE(%s::bigint, -1) ORDER BY turns.id LIMIT %s",
    "item": "SELECT id, text, ts, status FROM daybank_items "
            "WHERE id > COALESCE(%s, '') ORDER BY daybank_items.id LIMIT %s",
    "summary": "SELECT id::text AS native_id, text, ts, kind FROM summaries "
               "WHERE kind <> 'ace_profile' AND id > COALESCE(%s::bigint, -1) "
               "ORDER BY summaries.id LIMIT %s",
    # EVERY PROFILE VERSION IS ITS OWN DATED SOURCE. `set_profile` appends a new
    # `ace_profile` row rather than editing one, so the history is real history. Indexing
    # only the newest as a single `profile:current` would have made the profile the one
    # MUTABLE source in the layer: a source_id whose content silently changes underneath
    # every link and every quoted excerpt that points at it. Each version keeps the
    # summaries row id, so a dossier can say which wording it is quoting and when.
    "profile": "SELECT id::text AS native_id, text, ts FROM summaries "
               "WHERE kind = 'ace_profile' AND id > COALESCE(%s::bigint, -1) "
               "ORDER BY summaries.id LIMIT %s",
}


def read_corpus(corpus: str, limit: int = 500, since_native_id=None, cur=None) -> list:
    """A batch of source records, ordered by a STABLE key.

    The board's id is random hex, so ordering by it is arbitrary — but a resumable cursor
    needs a total order, not a chronological one, and an arbitrary stable order is a
    correct cursor. Chronology comes from `occurred_at`, which is stored per source.
    """
    out = []

    def _load(c):
        sql = _READ_SQL.get(corpus)
        if not sql:
            return
        c.execute(sql, (since_native_id, int(limit)))
        for r in c.fetchall():
            if corpus == "profile":
                # Brady's own editable self-description, so `user_statement` rather
                # than a summary — one source per stored version.
                out.append({"corpus": "profile", "native_id": r[0], "text": r[1],
                            "occurred_at": r[2], "role": "profile",
                            "kind": "ace_profile", "source_name": None})
                continue
            if corpus == "fact":
                out.append({"corpus": "fact", "native_id": r[0], "text": r[1],
                            "occurred_at": r[2], "role": "fact", "kind": None,
                            "source_name": r[3], "tier": r[4], "invalid_at": r[5]})
            elif corpus == "turn":
                out.append({"corpus": "turn", "native_id": r[0], "text": r[1],
                            "occurred_at": r[2], "role": r[3], "kind": None,
                            "source_name": r[4]})
            elif corpus == "item":
                out.append({"corpus": "item", "native_id": r[0], "text": r[1],
                            "occurred_at": r[2], "role": "board", "kind": None,
                            "source_name": None, "status": r[3]})
            else:
                out.append({"corpus": "summary", "native_id": r[0], "text": r[1],
                            "occurred_at": r[2], "role": "system", "kind": r[3],
                            "source_name": None})

    try:
        if cur is not None:
            _load(cur)
        else:
            with db._conn() as c, c.cursor() as cu:
                _load(cu)
    except Exception as e:
        logger.warning("entity_index read_corpus(%s) failed: %s", corpus, type(e).__name__)
        return []
    return out


def read_one(corpus: str, native_id, cur=None):
    """One source record, or None. Used by `note()`."""
    sql = {
        "fact": "SELECT id::text, text, ts, source, tier, invalid_at FROM facts WHERE id = %s",
        "turn": "SELECT id::text, content, ts, role, source FROM turns WHERE id = %s",
        "item": "SELECT id, text, ts, status FROM daybank_items WHERE id = %s",
        "summary": "SELECT id::text, text, ts, kind FROM summaries WHERE id = %s",
        "profile": "SELECT id::text, text, ts FROM summaries "
                   "WHERE id = %s AND kind = 'ace_profile'",
    }.get(corpus)
    if not sql:
        return None

    def _load(c):
        if corpus in ("fact", "turn", "summary", "profile"):
            try:
                c.execute(sql, (int(native_id),))
            except (TypeError, ValueError):
                return None
        else:
            c.execute(sql, (str(native_id),))
        r = c.fetchone()
        if not r:
            return None
        if corpus == "fact":
            return {"corpus": "fact", "native_id": r[0], "text": r[1], "occurred_at": r[2],
                    "role": "fact", "kind": None, "source_name": r[3], "tier": r[4],
                    "invalid_at": r[5]}
        if corpus == "turn":
            return {"corpus": "turn", "native_id": r[0], "text": r[1], "occurred_at": r[2],
                    "role": r[3], "kind": None, "source_name": r[4]}
        if corpus == "item":
            return {"corpus": "item", "native_id": r[0], "text": r[1], "occurred_at": r[2],
                    "role": "board", "kind": None, "source_name": None, "status": r[3]}
        if corpus == "profile":
            return {"corpus": "profile", "native_id": r[0], "text": r[1],
                    "occurred_at": r[2], "role": "profile", "kind": "ace_profile",
                    "source_name": None}
        return {"corpus": "summary", "native_id": r[0], "text": r[1], "occurred_at": r[2],
                "role": "system", "kind": r[3], "source_name": None}

    try:
        if cur is not None:
            return _load(cur)
        with db._conn() as c, c.cursor() as cu:
            return _load(cu)
    except Exception as e:
        logger.warning("entity_index read_one(%s) failed: %s", corpus, type(e).__name__)
        return None


def corpus_total(corpus: str, cur=None) -> int:
    sql = {"fact": "SELECT count(*) FROM facts",
           "turn": "SELECT count(*) FROM turns",
           "item": "SELECT count(*) FROM daybank_items",
           "summary": "SELECT count(*) FROM summaries WHERE kind <> 'ace_profile'",
           "profile": "SELECT count(*) FROM summaries WHERE kind = 'ace_profile'"}.get(corpus)
    if not sql:
        return 0

    def _load(c):
        c.execute(sql)
        return int(c.fetchone()[0] or 0)
    try:
        if cur is not None:
            return _load(cur)
        with db._conn() as c, c.cursor() as cu:
            return _load(cu)
    except Exception:
        return 0


# ── Candidate extraction — deterministic, no model ──────────────────────────────
# A capitalized run. Tokens must look like names, not like SHOUTING or code, and the
# handful of connectors real names contain are allowed in the middle.
_NAME_TOKEN = r"[A-Z][a-z][A-Za-z'’\-]*"
_CONNECTOR = r"(?:de|van|von|der|den|del|della|di|da|la|le|du|bin|al)"
# A trailing ALL-CAPS legal suffix. Without it "Halcyon Partners LLC" was captured as
# "Halcyon Partners" and the strongest type evidence in the string was discarded.
_LEGAL_SUFFIX = r"(?:LLC|L\.L\.C\.?|INC|LTD|LLP|PLLC|LP|PC|CO)"
NAME_SPAN_RE = re.compile(
    r"\b(%s(?:\s+(?:%s|%s)){0,3}(?:\s+%s)?)\b"
    % (_NAME_TOKEN, _NAME_TOKEN, _CONNECTOR, _LEGAL_SUFFIX))

# Words that are capitalized because a sentence started, not because they name anybody.
# This list is the difference between a review queue a human can work and four thousand
# rows of noise. It is deliberately generic English + calendar + this app's own
# vocabulary; nothing in it is specific to a real person.
_STOP_WORDS = set("""
the a an and or but so then than that this these those there here it its it's i i'm i've
he she they we you your yours my mine our ours his her hers their theirs them him us me
is are was were be been being am do does did done doing have has had having will would
can could should shall may might must need needs needed want wants wanted let lets
get got getting go goes going went gone come comes coming came make makes making made
say says saying said tell tells telling told ask asks asking asked see sees seeing saw
know knows knowing knew think thinks thinking thought take takes taking took
if when while because before after until unless since about above below under over
also just only even still yet already always never sometimes often usually maybe perhaps
yes no not none nothing something anything everything someone anyone everyone nobody
good great nice thanks thank please sorry hello hi hey ok okay sure right left
today tomorrow tonight yesterday morning afternoon evening night week weekend month year
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thurs fri sat sun
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
new old next last first second third final full half more less most least many few
what who whom whose which where why how whatever whenever however
update updated updates note notes noted reminder reminders follow followup
add added adding remove removed set setting settings done open close closed
board today's brady ace nothing confirmed completed pending waiting
learning memory sweep saved saving save file files
""".split())

# A trailing word that says "this is an organization". Amendment 1 is explicit that an
# org type needs EXPLICIT evidence or human review — a legal suffix is that evidence, and
# absent one a name stays `person` and stays unreviewed.
# Widened well past legal suffixes after the real copy filed `Allianz Life`, `Ally Bank`
# and `Amazon Prime` as people: in an insurance-and-contracting corpus the commercial
# vocabulary IS the evidence, and a trailing "Life" or "Bank" is worth more than any
# amount of capitalization.
_ORG_SUFFIX = ("llc", "l.l.c", "inc", "inc.", "corp", "corp.", "corporation", "company",
               "co", "co.", "ltd", "limited", "group", "partners", "holdings", "capital",
               "bank", "insurance", "agency", "associates", "foundation", "institute",
               "university", "college", "hospital", "clinic", "church", "trust", "fund",
               "life", "financial", "finance", "mortgage", "realty", "properties",
               "solutions", "services", "systems", "technologies", "technology", "tech",
               "health", "healthcare", "medical", "motors", "auto", "automotive",
               "energy", "logistics", "media", "labs", "laboratories", "ventures",
               "equity", "advisors", "advisory", "consulting", "consultants", "title",
               "escrow", "credit", "union", "savings", "express", "prime", "legends",
               "brokerage", "brokers", "underwriters", "mutual", "assurance", "annuity",
               "supply", "materials", "concrete", "construction", "contracting",
               "builders", "electric", "plumbing", "roofing", "landscaping", "studios",
               "network", "networks", "enterprises", "industries", "investments",
               "investment")

# US state and common place words. Belt and braces, and only ever used to BLOCK a person
# promotion — "South Carolina" was being filed as a person. A place may still become an
# org on org evidence, and any of these can still be a review candidate.
_PLACE_WORDS = set("""
alabama alaska arizona arkansas california colorado connecticut delaware florida georgia
hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland
massachusetts michigan minnesota mississippi missouri montana nebraska nevada hampshire
jersey mexico york carolina dakota ohio oklahoma oregon pennsylvania rhode tennessee
texas utah vermont virginia washington wisconsin wyoming county city township village
street road avenue drive lane court boulevard highway route north south east west
""".split())


def org_has_distinct_token(span: str) -> bool:
    """True when an org-looking span contains something other than company words.

    "Life Insurance", "Financial Services" and "Financial Group" are industries, not
    organizations, and the suffix rule promoted all three on the real corpus. An actual
    company name carries at least one token that is not itself a company word —
    "Allianz" in Allianz Life, "First" in First Bank.
    """
    toks = [t.casefold().strip(".,") for t in span.split()]
    return any(t not in set(_ORG_SUFFIX) | {"family", "small", "large", "local", "regional", "national"} for t in toks)


def looks_like_place(span: str) -> bool:
    toks = [t.casefold().strip(".,") for t in span.split()]
    return any(t in _PLACE_WORDS for t in toks)

# Relationship nouns Brady actually uses about people. Used for two things: the "my <rel>
# <Name>" introduction pattern, and the relationship-only reference ("my aunt") that
# names nobody and must therefore resolve to nobody.
RELATION_WORDS = ("aunt", "uncle", "cousin", "sister", "brother", "mother", "father",
                  "mom", "dad", "wife", "husband", "partner", "son", "daughter", "nephew",
                  "niece", "grandmother", "grandfather", "client", "customer", "agent",
                  "broker", "lawyer", "attorney", "accountant", "neighbor", "neighbour",
                  "friend", "boss", "manager", "contractor", "realtor", "recruiter",
                  "landlord", "tenant", "doctor", "dentist", "supervisor", "colleague",
                  "coworker", "co-worker", "buddy", "mentor", "assistant")
_REL_ALT = "|".join(sorted(RELATION_WORDS, key=len, reverse=True))

_INTRO_IS_MY = re.compile(
    r"\b(?P<name>%s(?:\s+%s){0,2})\s+is\s+my\s+(?P<rel>[a-z][a-z \-]{2,24})"
    % (_NAME_TOKEN, _NAME_TOKEN))
_INTRO_MY_REL = re.compile(
    r"\bmy\s+(?P<rel>%s)\s+(?P<name>%s(?:\s+%s){0,2})\b" % (_REL_ALT, _NAME_TOKEN,
                                                            _NAME_TOKEN))
_INTRO_MET = re.compile(
    r"\bmet\s+(?P<name>%s(?:\s+%s){0,2})\s+(?:from|at)\s+(?P<org>%s(?:\s+%s){0,3})\b"
    % (_NAME_TOKEN, _NAME_TOKEN, _NAME_TOKEN, _NAME_TOKEN))
# A relationship that names nobody. "my aunt" is a real person Brady is talking about and
# a mention this index cannot resolve — so it is raised, not dropped, and never guessed.
_REL_ONLY = re.compile(r"\bmy\s+(?P<rel>%s)\b(?!\s+%s)" % (_REL_ALT, _NAME_TOKEN))
_PROJECT_RE = re.compile(
    r"\bthe\s+(?P<name>%s(?:\s+%s){0,2})\s+(?:project|build|deal|renovation|remodel)\b"
    % (_NAME_TOKEN, _NAME_TOKEN))

# Plans and intentions. A plan is NOT a completion (amendment 1 C) and must never be
# stored as a state — "I'll send Chris the packet" is a thing Brady means to do.
PLAN_RE = re.compile(
    r"\b(i'?ll|i will|i'?m going to|im going to|going to|planning to|plan to|need to|"
    r"have to|gonna|want to|about to|will be)\b", re.I)
_STATUS_RE = re.compile(
    r"\b(sent|mailed|submitted|paid|signed|filed|delivered|finished|completed|closed)\b",
    re.I)
_ORG_ATTR_RE = re.compile(r"\b(works? (?:at|for)|employed (?:at|by)|owns|runs)\b", re.I)
_ROLE_ATTR_RE = re.compile(r"\b(is (?:an?|the) [a-z])", re.I)
_LOCATION_RE = re.compile(r"\b(lives in|based in|moved to|located in)\b", re.I)
_CONTACT_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}|\b\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}\b")


# ── CORPUS CASE STATISTICS — the fix for "Business Review" the person ──────────
#
# WHY THIS EXISTS (2026-09-21, blocker from the real copy). The first version of this
# migration promoted any capitalized span seen in two sources, and defaulted everything
# without a legal suffix to `person`. Run against Brady's real corpus it produced 283
# "people" including `Account Manager`, `Business Review`, `Build Team`, `Base Shop`,
# `Best Use Case Column`, `Call Armando` and `Call Jordan`, and it filed the companies
# `Allianz Life`, `Ally Bank` and `Amazon Prime` as PEOPLE. That is the old LLM-graph
# failure reproduced deterministically, which is worse than the LLM doing it: it is
# repeatable, confident and wrong.
#
# Two things were wrong. A capitalized span repeated twice is not a person — it is
# usually a heading, a board title or a sentence-initial verb. And "no legal suffix"
# is not evidence of personhood.
#
# So the corpus tells us which words are actually names. One pass counts, per token, how
# often it appears lowercase versus capitalized across every non-excluded source. A token
# Brady writes in lower case ("call", "review", "base", "column") is a COMMON WORD
# wherever it appears, including at the start of a sentence. A token that is essentially
# never lower case is CASE-DISTINCTIVE, and only those can be part of a name. This is
# measured from the data, not asserted by a list; the small list below is belt and braces.
COMMON_RATIO = 0.30         # lower/(lower+upper) at or above this ⇒ common
COMMON_LOWER_MIN = 3        # this many lowercase sightings ⇒ common, IF the ratio agrees
COMMON_ABS_RATIO = 0.10     # ...and "agrees" means at least this lowercase share
NAME_TOKEN_RATIO = 0.15     # below this ⇒ case-distinctive enough to be part of a name
# ⚠ THE ABSOLUTE FLOOR NEEDS THE RATIO GATE. A bare "lowercase >= 3 ⇒ common" rule was
# tried first and it deleted the real people: measured on the live corpus, `ken` appears
# lowercase 3 times against 366 capitalized, `chris` 6 against 626, `damon` 17 against
# 680 — a handful of transcription slips in four thousand turns. It cut the population
# from 283 wrong people to 6, which is a different way of being useless. The ratio is
# what actually separates a word from a name: `call` is 0.89 lowercase, `review` 0.92,
# `base` 0.82, `bank` 0.64 — and `ken` is 0.008.

# Belt and braces only. The statistics above are the mechanism; if a corpus is small
# enough that they have nothing to say, these still must never become a person.
_NEVER_A_NAME = set("""
account manager review team build base cost column analysis shop country call note
update task item board list card status priority report summary session plan project
meeting call-back inbox goal goals money bills admin business tech personal agents
deals networking opportunities morning evening today tomorrow week month year
""".split())


class CaseStats:
    """Lower/upper counts per token across the corpus. Deterministic, one pass, no model."""

    __slots__ = ("lower", "upper", "sources")

    def __init__(self):
        self.lower = {}
        self.upper = {}
        self.sources = 0

    def add_text(self, text: str) -> None:
        for tok in _CASE_TOKEN_RE.findall(str(text or "")):
            key = tok.casefold()
            if tok[:1].isupper():
                self.upper[key] = self.upper.get(key, 0) + 1
            elif tok.islower():
                self.lower[key] = self.lower.get(key, 0) + 1
        self.sources += 1

    def ratio(self, token: str) -> float:
        key = token.casefold()
        lo, up = self.lower.get(key, 0), self.upper.get(key, 0)
        return (lo / float(lo + up)) if (lo + up) else 0.0

    def is_common(self, token: str) -> bool:
        key = token.casefold()
        if key in _STOP_WORDS or key in _NEVER_A_NAME:
            return True
        r = self.ratio(key)
        if r >= COMMON_RATIO:
            return True
        return self.lower.get(key, 0) >= COMMON_LOWER_MIN and r >= COMMON_ABS_RATIO

    def is_name_token(self, token: str) -> bool:
        key = token.casefold()
        if self.is_common(key):
            return False
        return self.ratio(key) < NAME_TOKEN_RATIO

    def trim(self, span: str) -> str:
        """Strip common words from the EDGES of a span.

        "Call Armando" → "Armando" (which is then only one token, so it is a review
        candidate and not a person). "Business Review" → "" — nothing is left, so it was
        never a name at all. Interior tokens are left alone: "Kim de Vries" keeps its
        connector.

        A span ending in a company word is NOT trimmed: organizations are allowed to be
        made of ordinary words, and trimming "Ally Bank" to nothing would lose the one
        candidate whose type we can actually evidence.
        """
        toks = _strip_possessive(span).split()
        if has_org_suffix(span):
            # Only a leading ACTION VERB is noise here ("Call First Bank" → "First
            # Bank"). Trimming every common word off the front ate "First" and left a
            # one-token "Bank", which then failed the two-token org bar — a company
            # name is allowed to be made of ordinary words.
            while len(toks) > 1 and toks[0].casefold() in _LEADING_VERBS:
                toks.pop(0)
            return " ".join(toks)
        while toks and self.is_common(toks[0]):
            toks.pop(0)
        while toks and self.is_common(toks[-1]):
            toks.pop()
        return " ".join(toks)


_CASE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]{1,}")
_TRAILING_POSSESSIVE_RE = re.compile(r"['’]s\b")
_LEADING_VERBS = frozenset((
    "call", "called", "email", "emailed", "text", "texted", "meet", "contact", "check",
    "pay", "paid", "review", "update", "send", "sent", "ask", "follow", "schedule",
    "confirm", "cancel", "book", "finish", "start", "get", "set"))


def _strip_possessive(span: str) -> str:
    """"Rebecca's packet" yields the span "Rebecca's"; the person is Rebecca.

    `norm_alias` already folds the possessive away for the KEY, so without this the key
    and the display name disagreed and Brady was shown an entity called "Rebecca's".
    """
    return " ".join(re.split(r"[’\']s\b", span, maxsplit=1)[0].split())


def _name_ok(span: str) -> bool:
    toks = span.split()
    if not toks or len(toks) > 4:
        return False
    if any(t.casefold() in _STOP_WORDS for t in toks):
        return False
    if len(toks) == 1:
        return False            # a bare capitalized word is not evidence of a person
    return True


def name_spans(text: str) -> list:
    """Multi-token capitalized spans that could be a name. CANDIDATES, not entities.

    Everything this returns still has to clear the creation bar (two distinct sources, or
    an explicit introduction) before it becomes anything. A span that never clears it
    becomes a review row, not a silent omission.
    """
    out, seen = [], set()
    for m in NAME_SPAN_RE.finditer(str(text or "")):
        span = " ".join(m.group(1).split())
        if not _name_ok(span):
            continue
        key = entities.norm_alias(span)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(span)
    return out


def has_org_suffix(span: str) -> bool:
    toks = [t.casefold().strip(".,") for t in span.split()]
    return bool(toks) and toks[-1] in _ORG_SUFFIX


def guess_type(span: str) -> str:
    """'org' on explicit commercial evidence, else '' — NEVER a person by default.

    The old version returned "person" for anything without a legal suffix, which is how
    `Allianz Life`, `Ally Bank` and `Amazon Prime` became people. Personhood now has to
    be earned from context (see `person_context`); absent that, a span has no type at all
    and stays a review candidate with `claimed_type='unknown'`.
    """
    return "org" if has_org_suffix(span) else ""


# What is going on AROUND a name is the evidence, not the capitalization itself.
# "called Jordan", "Jordan said", "Jordan's packet", "my aunt Jordan" are things people
# do to and with PEOPLE. A heading is none of them.
_PRE_PERSON_RE = re.compile(
    r"\b(call|called|calling|text|texted|texting|email|emailed|meet|met|meeting|"
    r"spoke to|spoke with|talked to|talk to|ask|asked|tell|told|with|from|for|see|saw|"
    r"visit|visited|thank|thanked|remind|reminded|hire|hired|introduced|named|"
    r"referred by|referral from|send|sent|mail|mailed|give|gave|show|showed|bring|"
    r"brought|owe|owes|pay|paid|help|helped|my\s+\w+)\s+$", re.I)
_POST_PERSON_RE = re.compile(
    r"^(?:['’]s\b|\s+(said|says|told|asked|calls|called|texted|emailed|sent|wants|"
    r"needs|will|is|was|has|had|can|should|thinks|mentioned|replied|responded|"
    r"confirmed|agreed|signed|owes|owns)\b)", re.I)
# Deliberately NARROW. A first cut included "through", "contract with" and a trailing
# "plan|quote|account", and the real corpus promptly typed Damon, Gabby and Chris — three
# people — as organizations, because you go "through Damon" and discuss "Chris account".
# An org phrase has to be one that is almost never said about a person.
_PRE_ORG_RE = re.compile(
    r"\b(works? at|works? for|working at|employed at|employed by|policy with|"
    r"policy through|underwritten by|carrier is|appointed with)\s+$", re.I)
_POST_ORG_RE = re.compile(
    r"^\s+(is the carrier|is (?:an? )?(?:company|organization|business))\b",
    re.I)

_CTX_WINDOW = 40


def person_context(text: str, start: int, end: int) -> bool:
    """True when the span at [start:end) is used the way a PERSON is used."""
    pre = text[max(0, start - _CTX_WINDOW):start]
    post = text[end:end + _CTX_WINDOW]
    return bool(_PRE_PERSON_RE.search(pre) or _POST_PERSON_RE.match(post))


def org_context(text: str, start: int, end: int) -> bool:
    pre = text[max(0, start - _CTX_WINDOW):start]
    post = text[end:end + _CTX_WINDOW]
    return bool(_PRE_ORG_RE.search(pre) or _POST_ORG_RE.match(post))


def span_occurrences(text: str):
    """(raw_span, start, end, person_ctx, org_ctx) for every capitalized run.

    Untrimmed and unjudged: trimming needs corpus-wide case statistics that do not exist
    until the whole scan is done, so this stage only observes.
    """
    t = str(text or "")
    for m in NAME_SPAN_RE.finditer(t):
        raw = m.group(1)
        if len(raw.split()) > 4:
            continue
        yield (" ".join(raw.split()), m.start(1), m.end(1),
               person_context(t, m.start(1), m.end(1)),
               org_context(t, m.start(1), m.end(1)))


def introductions(text: str) -> list:
    """Explicit introductions in a source. [{name, type, relation, org, pattern}]."""
    out = []
    t = str(text or "")
    for m in _INTRO_IS_MY.finditer(t):
        name = " ".join(m.group("name").split())
        if _name_ok(name) or len(name.split()) == 1:
            out.append({"name": name, "type": guess_type(name) or "person",
                        "relation": m.group("rel").strip(), "org": None,
                        "pattern": "is_my"})
    for m in _INTRO_MY_REL.finditer(t):
        name = " ".join(m.group("name").split())
        out.append({"name": name, "type": "person",
                    "relation": m.group("rel").strip(), "org": None,
                    "pattern": "my_rel_name"})
    for m in _INTRO_MET.finditer(t):
        name = " ".join(m.group("name").split())
        org = " ".join(m.group("org").split())
        out.append({"name": name, "type": "person", "relation": None, "org": org,
                    "pattern": "met_at"})
    for m in _PROJECT_RE.finditer(t):
        name = " ".join(m.group("name").split())
        if _name_ok(name) or len(name.split()) == 1:
            out.append({"name": name, "type": "project", "relation": None, "org": None,
                        "pattern": "project"})
    # Drop introductions whose "name" is a stopword run ("The Next is my …").
    return [i for i in out
            if i["name"] and not any(t.casefold() in _STOP_WORDS
                                     for t in i["name"].split())]


def relationship_refs(text: str) -> list:
    """['my aunt', …] — references to a person by relationship, naming nobody."""
    out, seen = [], set()
    for m in _REL_ONLY.finditer(str(text or "")):
        phrase = "my " + m.group("rel").casefold()
        if phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def attribute_of(sentence: str) -> str:
    """Which attribute a quoted sentence is about. A PLAN IS NEVER A STATUS."""
    if PLAN_RE.search(sentence):
        return "plan"
    if _CONTACT_RE.search(sentence):
        return "contact"
    if _ORG_ATTR_RE.search(sentence):
        return "org"
    if _LOCATION_RE.search(sentence):
        return "location"
    if _STATUS_RE.search(sentence):
        return "status"
    if _ROLE_ATTR_RE.search(sentence):
        return "role"
    return "note"


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")


def sentences(text: str, limit: int = 40) -> list:
    return [s.strip() for s in _SENTENCE_RE.findall(str(text or ""))[:limit] if s.strip()]


def _learn_introductions_cur(cur, rec, sid, index):
    """A direct introduction can create an unreviewed record, scoped to this source.

    No first-name alias is globally confirmed. Existing ambiguous/rejected identities
    are not merged or resurrected, and assistant narration cannot introduce a person.
    """
    if rec.get("role") not in ("user", "profile"):
        return
    for intro in introductions(rec.get("text") or "")[:8]:
        name = _strip_possessive(intro["name"])
        if not name:
            continue
        verdict, candidates = entities.resolve_in_index(name, index)
        if verdict == "ambiguous":
            continue
        kind = intro.get("type") or "person"
        if verdict == "resolved":
            eid = candidates
        else:
            eid, _ = entities.upsert_entity_cur(
                cur, kind, name, origin="direct_introduction", confidence=.6,
                review_status="unreviewed", source_id=sid,
                occurred_at=rec.get("occurred_at"),
                key=entities.import_key(kind, entities.norm_alias(name)),
                first_name_aliases=[name.split()[0]] if kind == "person" and len(name.split()) > 1 else ())
            cur.execute("SELECT status, review_status FROM ace_entities WHERE entity_id=%s", (eid,))
            state = cur.fetchone()
            if not state or state[0] != "active" or state[1] == "rejected":
                continue
            index.clear()
            index.update(entities.alias_index(cur))
        entities.link_cur(cur, eid, sid, relation="introduced", method="introduction",
                          origin="user_statement", confidence=.7, evidence=name,
                          review_status="unreviewed")
        if intro.get("relation"):
            entities.add_entity_fact_cur(
                cur, eid, "relationship", name + " — " + intro["relation"],
                stated_at=rec.get("occurred_at"), source_id=sid, origin="user_statement",
                source_class="user_statement", confidence=.7, supersede=False)


# ── Indexing one source ─────────────────────────────────────────────────────────
def index_source_cur(cur, rec: dict, index: dict, *, queue: bool = True,
                     origin: str = "migration", stats: dict = None) -> dict:
    """Index one source and link it. THE single path — `note()` and the migration
    both call this, so a hook and a backfill cannot disagree about what a source means.

    Returns {source_id, status, links_new, ambiguous, tombstoned, excluded}.
    """
    st = stats if stats is not None else {}

    def bump(k, n=1):
        st[k] = st.get(k, 0) + n

    text = rec.get("text") or ""
    corpus = rec.get("corpus")
    cls, excl = entities.classify_source(
        corpus, role=rec.get("role"), kind=rec.get("kind"),
        source_name=rec.get("source_name"), text=text)

    if excl:
        # Still indexed, still counted, still searchable. `excluded_reason` keeps it out
        # of dossiers, prompts and learning — and out of the alias scan, which is what
        # stops a save-narration's co-mentions being read as a relationship.
        sid, created, _changed = entities.note_source_cur(
            cur, corpus, rec["native_id"], text, occurred_at=rec.get("occurred_at"),
            role=rec.get("role"), status="excluded", source_class=cls,
            excluded_reason=excl)
        bump("scanned"); bump("excluded")
        bump("excluded:" + excl)
        if created:
            bump("sources_new")
        return {"source_id": sid, "status": "excluded", "links_new": 0, "ambiguous": 0,
                "tombstoned": 0, "excluded": True, "source_class": cls, "resolved": []}

    sid, created, changed = entities.note_source_cur(
        cur, corpus, rec["native_id"], text, occurred_at=rec.get("occurred_at"),
        role=rec.get("role"), status="indexed", source_class=cls, excluded_reason=None)
    bump("scanned")
    if created:
        bump("sources_new")
    if changed:
        bump("sources_changed")
    if not sid:
        return {"source_id": "", "status": "", "links_new": 0, "ambiguous": 0,
                "tombstoned": 0, "excluded": False, "source_class": cls, "resolved": []}

    if origin != "migration" and cls == "user_statement":
        _learn_introductions_cur(cur, rec, sid, index)

    cur.execute("SELECT entity_id, relation, retracted_at FROM ace_entity_links "
                "WHERE source_id = %s", (sid,))
    prior = {(r[0], r[1]): r[2] for r in cur.fetchall()}

    hits = entities.scan_aliases(text, index)
    resolved, ambiguous = [], []
    for verdict, key, ids, raw in hits:
        if verdict == "resolved" and ids:
            if ids[0] not in [r[0] for r in resolved]:
                resolved.append((ids[0], raw))
        elif verdict == "ambiguous":
            ambiguous.append((key, ids, raw))
    refs = relationship_refs(text)

    links_new, tombstoned = 0, 0
    for eid, raw in resolved:
        was = prior.get((eid, "mentions"), "absent")
        if was != "absent":
            if was is not None:
                tombstoned += 1        # a human removed this mapping; it stays removed
            continue                   # already linked, nothing to do
        link_id, made = entities.link_cur(
            cur, eid, sid, relation="mentions", method="exact_alias", origin=origin,
            confidence=0.6, evidence=raw)
        if made:
            links_new += 1
        elif link_id is None:
            tombstoned += 1

    if changed:
        # The original was edited. Retract only the machine links the NEW text no longer
        # supports; a link that is still supported keeps its history.
        n = entities.retract_stale_links_cur(
            cur, sid, [e for e, _ in resolved],
            "source_changed: the alias is no longer present in the current text")
        if n:
            bump("links_retracted_stale", n)

    # ⚠ COUNT LIVE LINKS *AFTER* THE RETRACTION, AND ASK THE DATABASE.
    #
    # This used to be a running tally that added the source's PRIOR live links before
    # the retraction above removed them. Edit a board row from "Call Jordan Rivera
    # today" to "Buy milk" and the re-index left it `status='indexed'` with zero live
    # links: it broke the partition this whole report rests on (indexed means at least
    # one link), it fell out of the `unassigned` population, and it therefore never got
    # an `unassigned_source` review row — so editing a name out of a record made that
    # record silently unreachable from the review path. The four-bucket arithmetic still
    # balanced, which is exactly why the report could not catch it. One authoritative
    # count, after every mutation, costs one query and cannot drift.
    cur.execute("SELECT count(*) FROM ace_entity_links WHERE source_id = %s "
                "AND retracted_at IS NULL", (sid,))
    live = int(cur.fetchone()[0] or 0)

    if live > 0:
        status = "indexed"
    elif ambiguous or refs:
        status = "ambiguous"
    else:
        status = "unassigned"
    entities.set_source_status_cur(cur, sid, status)
    bump("status:" + status)
    if links_new:
        bump("links_new", links_new)
    if tombstoned:
        bump("tombstones_respected", tombstoned)

    if queue:
        _queue_unresolved(cur, sid, rec, text, ambiguous, refs, status,
                          bool(tombstoned), bump)
    if origin != "migration":
        # The migration and ongoing writes share the same bounded, source-only parser.
        from . import entity_migrate
        entity_migrate._entity_facts_for(cur, rec, sid, cls, index, st)
    cur.execute("UPDATE ace_entities e SET last_seen=GREATEST(e.last_seen,%s::timestamptz) "
                "WHERE e.review_status<>'rejected' AND EXISTS (SELECT 1 FROM ace_entity_links l "
                "WHERE l.entity_id=e.entity_id AND l.source_id=%s AND l.retracted_at IS NULL)",
                (rec.get("occurred_at"), sid))
    return {"source_id": sid, "status": status, "links_new": links_new,
            "ambiguous": len(ambiguous), "tombstoned": tombstoned, "excluded": False,
            "source_class": cls, "resolved": [e for e, _ in resolved]}


def _queue_unresolved(cur, sid, rec, text, ambiguous, refs, status, had_tombstone, bump):
    """Make every unresolved thing VISIBLE, and keep the queue small enough to work.

    Deduped by subject: one row per ambiguous NAME and one per relationship phrase, not
    one per occurrence — four thousand rows nobody reads is the same as hiding them.
    """
    for key, ids, raw in ambiguous[:5]:
        _, made = entities.queue_review_cur(
            cur, "ambiguous_name", key,
            {"alias": raw, "alias_norm": key, "entity_ids": ids,
             "why": ("this name matches %d known entities, or is a first name that has "
                     "never been confirmed; no link was written" % len(ids)),
             "example_source_id": sid},
            priority=entities.review_priority("ambiguous_name"))
        if made:
            bump("review_new"); bump("review:ambiguous_name")
    for phrase in refs[:3]:
        _, made = entities.queue_review_cur(
            cur, "ambiguous_name", "relationship:" + phrase,
            {"alias": phrase, "entity_ids": [],
             "why": ("a relationship reference that names nobody; it was linked to no "
                     "entity and nothing was guessed"),
             "example_source_id": sid},
            priority=entities.review_priority("ambiguous_name"))
        if made:
            bump("review_new"); bump("review:ambiguous_name")
    # Unassigned sources remain searchable and counted. Repetition of an unknown name
    # is grouped by the candidate migration; one review per source would create hundreds
    # of duplicate chores without adding identity evidence.


# ── THE HOOK ────────────────────────────────────────────────────────────────────
#
# ⚠ note() DOES NO DATABASE WORK ON THE CALLER'S THREAD.
#
# It used to. It ran `db._conn()` inline at the end of `add_fact` / `append_turn` /
# `add_item` / `update_item`, and db's pool semaphore waits up to ten seconds for a slot.
# So under pool pressure an index refresh could hold a VOICE TURN for ten seconds while
# the caller's `except Exception: pass` hid the fact that anything was happening. An
# index is never worth a second of Brady's turn, let alone ten.
#
# Now the hook is an enqueue and nothing else: bounded queue, no lock held, no DB call,
# returns immediately. A daemon worker drains it. If the queue is full the item is
# DROPPED and a counter goes up — dropping is safe because `reconcile()` sweeps by
# timestamp and content hash and picks up anything a hook missed, and blocking is not
# safe at all. The counter is exposed by `queue_stats()` so "we dropped some" is a number
# somebody can see rather than a silence.
NOTE_QUEUE_MAX = 500
NOTE_WORKER_TIMEOUT_MS = 5000          # statement_timeout for the worker's own DB work
_QUEUE = queue.Queue(maxsize=NOTE_QUEUE_MAX)
_WORKER = {"thread": None}
_WORKER_LOCK = threading.Lock()
_QSTATS = {"queued": 0, "dropped": 0, "indexed": 0, "failed": 0}


def queue_stats() -> dict:
    return dict(_QSTATS, depth=_QUEUE.qsize(), capacity=NOTE_QUEUE_MAX)


def note(corpus: str, native_id) -> None:
    """Index one newly-written row. Enqueue-only: returns in microseconds, never raises.

    Called from db.py inside `try/except Exception: pass`. The belt here is deliberate —
    the caller's except is the braces, and neither one may be the only guard on a path
    that runs on every single write to Ace's memory.
    """
    try:
        if corpus not in entities.CORPORA or not entities.enabled():
            return
        _ensure_worker()
        _QUEUE.put_nowait((corpus, str(native_id)))
        _QSTATS["queued"] += 1
    except queue.Full:
        # Visible, not silent. The reconciliation sweep is what makes this recoverable.
        _QSTATS["dropped"] += 1
    except Exception:                          # never, ever into a turn
        _QSTATS["dropped"] += 1
    return None


def _ensure_worker() -> None:
    t = _WORKER["thread"]
    if t is not None and t.is_alive():
        return
    with _WORKER_LOCK:
        t = _WORKER["thread"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_worker_loop, name="ace-entity-index", daemon=True)
        _WORKER["thread"] = t
        t.start()


def _worker_loop() -> None:
    while True:
        try:
            item = _QUEUE.get()
        except Exception:
            return
        try:
            _index_one(*item)
        except Exception as e:
            _QSTATS["failed"] += 1
            logger.debug("entity_index worker skipped %s: %s", item, type(e).__name__)
        finally:
            try:
                _QUEUE.task_done()
            except Exception:
                pass


def _index_one(corpus: str, native_id) -> bool:
    """One source, on the worker thread, with a hard statement timeout."""
    if not _layer_ready():
        return False
    with db._conn() as c, c.cursor() as cur:
        try:
            cur.execute("SET LOCAL statement_timeout = %s", (NOTE_WORKER_TIMEOUT_MS,))
        except Exception:
            pass
        rec = read_one(corpus, native_id, cur)
        if not rec:
            return False
        index = entities.alias_index(cur)
        index_source_cur(cur, rec, index, queue=True, origin="index")
    _QSTATS["indexed"] += 1
    return True


def drain(timeout: float = 10.0) -> bool:
    """Block until the queue is empty. For tests and for an orderly shutdown."""
    _ensure_worker()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _QUEUE.mutex:
            if _QUEUE.unfinished_tasks == 0:
                return True
        time.sleep(0.02)
    with _QUEUE.mutex:
        return _QUEUE.unfinished_tasks == 0


def index_batch(corpus: str, limit: int = 500, since_native_id=None) -> dict:
    """{scanned, indexed, linked, ambiguous, unassigned, excluded}. Resumable.

    One transaction for the batch: either the whole batch lands or none of it does, and
    the checkpoint moves only with it, so an interrupted run resumes without a gap and
    without a double-count.
    """
    out = {"scanned": 0, "indexed": 0, "linked": 0, "ambiguous": 0, "unassigned": 0,
           "excluded": 0, "corpus": corpus}
    if not entities.enabled() or corpus not in entities.CORPORA:
        return out
    try:
        with db._conn() as c, c.cursor() as cur:
            rows = read_corpus(corpus, limit, since_native_id, cur)
            if not rows:
                return out
            index = entities.alias_index(cur)
            stats, last = {}, None
            for rec in rows:
                index_source_cur(cur, rec, index, queue=True, origin="index", stats=stats)
                last = rec["native_id"]
            out["scanned"] = stats.get("scanned", 0)
            out["indexed"] = stats.get("status:indexed", 0)
            out["linked"] = stats.get("links_new", 0)
            out["ambiguous"] = stats.get("status:ambiguous", 0)
            out["unassigned"] = stats.get("status:unassigned", 0)
            out["excluded"] = stats.get("excluded", 0)
            entities.checkpoint_cur(cur, corpus, last_native_id=last,
                                    last_ts=rows[-1].get("occurred_at"), counts=out)
    except Exception as e:
        logger.warning("entity_index index_batch(%s) failed: %s: %s", corpus,
                       type(e).__name__, e)
    return out


def refresh_all(limit_per_corpus: int = 500) -> dict:
    """One bounded incremental pass over every corpus, resuming from the checkpoints."""
    out = {"corpora": {}, "scanned": 0, "linked": 0, "excluded": 0}
    for corpus in entities.CORPORA:
        cp = checkpoint(corpus)
        since = cp.get("last_native_id") if corpus != "profile" else None
        got = index_batch(corpus, limit_per_corpus, since)
        out["corpora"][corpus] = got
        out["scanned"] += got.get("scanned", 0)
        out["linked"] += got.get("linked", 0)
        out["excluded"] += got.get("excluded", 0)
    return out


# ── Reconciliation — what the id cursor cannot see ─────────────────────────────
#
# WHY THIS EXISTS. `refresh_all` walks a NATIVE-ID cursor, which only ever finds rows
# that are NEW. It structurally cannot see:
#   • a fact that was ARCHIVED after it was indexed (`facts.invalid_at` is set by
#     `archive_fact` / `set_fact_tier`; the row's id has not moved),
#   • a board row whose text was EDITED (its id is a random UUID that may sort anywhere,
#     so `id > checkpoint` skips it forever),
#   • anything a dropped `note()` missed.
# So an id cursor alone means a correction Brady makes today can be invisible to the
# index for good. These three sweeps are the recovery path, and they are cheap enough to
# run on a schedule: two are bounded by time, the third by the size of the board.
RECONCILE_WINDOW_HOURS = 72


def reconcile(hours: int = RECONCILE_WINDOW_HOURS, board_limit: int = 2000) -> dict:
    """Catch what the id cursor misses. Best-effort; returns what it did."""
    out = {"archived_facts_closed": 0, "board_rehashed": 0, "board_changed": 0,
           "recent_reindexed": 0, "errors": []}
    if not entities.enabled() or not _layer_ready():
        return out
    try:
        out["archived_facts_closed"] = _close_archived_facts()
    except Exception as e:
        out["errors"].append("archived: %s" % type(e).__name__)
    try:
        got = _board_hash_sweep(board_limit)
        out["board_rehashed"], out["board_changed"] = got
    except Exception as e:
        out["errors"].append("board: %s" % type(e).__name__)
    try:
        out["recent_reindexed"] = _recent_sweep(hours)
    except Exception as e:
        out["errors"].append("recent: %s" % type(e).__name__)
    return out


def _close_archived_facts() -> int:
    """An archived ORIGINAL retires the claims drawn from it — as dated history.

    No `superseded_by` is invented: 294 archived facts in the live corpus have none, and
    fabricating a replacement chain would be asserting a correction nobody made.
    """
    with db._conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ace_entity_facts ef SET valid_to = f.invalid_at "
            "FROM facts f WHERE ef.source_id = 'fact:' || f.id::text "
            "AND f.invalid_at IS NOT NULL AND ef.valid_to IS NULL")
        return cur.rowcount or 0


def _board_hash_sweep(limit: int) -> tuple:
    """Board ids are UUIDs, so no cursor orders them. Compare hashes instead."""
    checked = changed = 0
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT d.id, d.text, d.ts, s.content_hash FROM daybank_items d "
                    "LEFT JOIN ace_sources s ON s.source_id = 'item:' || d.id "
                    "ORDER BY d.updated_at DESC NULLS LAST, d.ts DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
        stale = [(r[0], r[1], r[2]) for r in rows
                 if r[3] is None or r[3] != entities.content_hash(r[1])]
        checked = len(rows)
        if stale:
            index = entities.alias_index(cur)
            for iid, text, ts in stale:
                index_source_cur(cur, {"corpus": "item", "native_id": iid, "text": text,
                                       "occurred_at": ts, "role": "board", "kind": None,
                                       "source_name": None},
                                 index, queue=True, origin="reconcile")
                changed += 1
    return checked, changed


def _recent_sweep(hours: int) -> int:
    """Re-index everything written or touched in the last `hours`, by TIMESTAMP.

    The time cursor runs alongside the id cursor precisely because they fail in
    different directions: ids miss edits, timestamps miss nothing recent.
    """
    n = 0
    sql = {
        "fact": "SELECT id::text AS native_id, text, ts, source, tier, invalid_at "
                "FROM facts WHERE ts > now() - make_interval(hours => %s) "
                "ORDER BY facts.id LIMIT 2000",
        "turn": "SELECT id::text AS native_id, content, ts, role, source FROM turns "
                "WHERE ts > now() - make_interval(hours => %s) "
                "ORDER BY turns.id LIMIT 2000",
        "summary": "SELECT id::text AS native_id, text, ts, kind FROM summaries "
                   "WHERE kind <> 'ace_profile' "
                   "AND ts > now() - make_interval(hours => %s) "
                   "ORDER BY summaries.id LIMIT 2000",
    }
    with db._conn() as c, c.cursor() as cur:
        index = entities.alias_index(cur)
        for corpus, q in sql.items():
            cur.execute(q, (int(hours),))
            for r in cur.fetchall():
                if corpus == "fact":
                    rec = {"corpus": "fact", "native_id": r[0], "text": r[1],
                           "occurred_at": r[2], "role": "fact", "kind": None,
                           "source_name": r[3], "tier": r[4], "invalid_at": r[5]}
                elif corpus == "turn":
                    rec = {"corpus": "turn", "native_id": r[0], "text": r[1],
                           "occurred_at": r[2], "role": r[3], "kind": None,
                           "source_name": r[4]}
                else:
                    rec = {"corpus": "summary", "native_id": r[0], "text": r[1],
                           "occurred_at": r[2], "role": "system", "kind": r[3],
                           "source_name": None}
                index_source_cur(cur, rec, index, queue=True, origin="reconcile")
                n += 1
        entities.checkpoint_cur(cur, "reconcile", last_native_id=None, last_ts=None,
                                counts={"reindexed": n, "hours": int(hours)})
    return n


def checkpoint(corpus: str) -> dict:
    def _read(cur):
        cur.execute("SELECT corpus, last_native_id, last_ts, index_version, counts, "
                    "updated_at FROM ace_index_checkpoint WHERE corpus = %s", (corpus,))
        r = cur.fetchone()
        if not r:
            return {}
        return {"corpus": r[0], "last_native_id": r[1],
                "last_ts": entities._iso(r[2]), "index_version": r[3], "counts": r[4],
                "updated_at": entities._iso(r[5])}
    return entities._run(_read, {}, "checkpoint") or {}
