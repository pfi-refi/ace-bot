# Final integration addendum — September 22, 2026

Target release: v2.1.0. This addendum supersedes preliminary measurements below.
Claude Code agents implemented the layer; independent Codex review corrected exact board
receipt verification, source-history/API outage handling, dated partial-context priorities,
existing-card notification modes, source-search paging, actual maintenance scheduling,
numbered save-narration filtering and contextual candidate quality. A bounded second review
corrected normalized alias retraction, rejected-entity resolution, source-state accounting,
constant-query registry loading, conflict truncation and source delimiter preservation.

Original-table hashes remain identical on the restored copy. Every one of 8,649 source rows
is accounted for; a replay adds no duplicate rows. Weak capitalization remains searchable
without creating one review chore per source. Automated candidate records remain unreviewed.
A separate private, source-checked curation adds focused mappings for core people/projects;
no private evidence, client records or credentials are committed to this repository.
No original board, profile, conversation or fact row is changed by the memory migration.

Production lifecycle now schedules one non-overlapping recovery pass every 15 minutes after
a 30-second startup delay. Per-write indexing uses a queue, never synchronous work on voice.
The old Drive/Telegram/archive recall path remains; those external archives are not newly
backfilled here. Physical microphone/ElevenLabs audio acceptance is not claimed by local tests.
Nigel may reuse this architecture with its own database and permissions; no Nigel/client system
or data is changed by this release.

See the delivery handoff for final deployed commit, backup, curation and acceptance results.
The rest of this file records the design and preliminary findings, not final population counts.

---

# Ace memory upgrade + receipt repair — September 21, 2026

Built on v2.0.5 (`d9a6c8d`). **Not deployed.** Everything here was developed and tested
locally against disposable PostgreSQL and a disposable restored COPY of production. Codex owns
the production backup, the final source-hash comparison, the deployment and the backfill.

This release adds an **additive entity memory layer** and repairs **two live defects** found in
the September 21 production test. It does not redesign the board, does not change personal or
client context storage, and does not claim the corpus is organized — see *Limitations*.

---

## 1. Architecture

### The shape of the thing

Ace's existing stores — `facts`, `turns`, `daybank_items`, `summaries`, the profile — remain
the record and are **never written by this release**. On top of them sits a mapping layer that
points at those rows **by id** and adds identity, provenance and review state.

```
  facts   turns   daybank_items   summaries   profile      ← ORIGINALS, untouched
     ▲       ▲          ▲             ▲          ▲
     └───────┴──────────┴─────────────┴──────────┘
                        │  source_id = "<corpus>:<native_id>"
                 ┌──────┴───────┐
                 │  ace_sources │  hash + class + status. NO source text is copied.
                 └──────┬───────┘
          ┌─────────────┼──────────────┬────────────────┐
   ace_entity_links  ace_entity_facts  ace_entity_review │
          │             │                                │
          └──────┬──────┴────────────────────────────────┘
            ace_entities ── ace_entity_aliases ── ace_entity_relations
                                                   ace_entity_audit
                                                   ace_index_checkpoint
```

Two properties follow from this shape and are the reason for it:

- **Rollback is dropping the new tables.** Nothing else has to be undone, because nothing else
  was changed.
- **Private data is not duplicated.** `ace_sources` stores a SHA-256 hash, a length, a class
  and timestamps — never the text. Every excerpt shown in a dossier or the UI is read live
  from the original table at render time, so the original remains the only copy.

### Why the source index stores a class, not just a role

The corpus is not uniform, and treating it as uniform is how a memory layer starts inventing
things. Measured on the real export:

| Population | Count | Consequence |
|---|---|---|
| user turns | 2 349 | direct testimony |
| assistant turns | 1 805 | Ace's own narration, not evidence |
| facts, `source='sweep'` | 1 338 | legacy extraction from conversation |
| facts, `source='reflection'` | 566 | assistant self-reflection |
| facts, other (`ace2`/`win`/`capture`) | 229 | legacy extraction |
| archived facts with **no** `superseded_by` | 294 | dated history, no replacement exists |
| summaries: `watch_state`/`watch_snapshot`/settings/etc. | 1 013 | operational telemetry |
| summaries: `recap`/`brief_morning`/`brief_eod` | 619 | secondary summaries |
| summaries: `graph_cache` | 181 | historical model-generated graph snapshots |
| summaries: `ace_profile` | 1 | the profile |
| QA/test-marked rows | 6 turns + 1 board row | synthetic, not personal knowledge |

The corpus totals **8 649 source records** (facts 2 133 + turns 4 154 + board 548 +
summaries 1 814), and the classification above partitions it exactly:
`2 349 + 548 + 1` user statement, `1 805 + 566 + 181` assistant inference, `1 567` legacy
extracted, `619` secondary summary, `1 013` internal metadata — summing to 8 649. The
migration report reconciles against that number arithmetically rather than asserting
completeness.

Note that the JSON export manifest counts summaries as 1 632 because it splits `ace_profile`
and the latest `graph_cache` into their own top-level keys; the database has 1 814.

Every source therefore carries `source_class` ∈ `user_statement | assistant_inference |
legacy_extracted | secondary_summary | internal_metadata | test_data`, plus an
`excluded_reason` when it must stay out of dossiers, prompts and learning.

**Excluded sources are still indexed, still counted, still searchable, still inspectable.**
Exclusion governs presentation, never accountability. Nothing disappears.

`facts.subject` and `facts.kind` are NULL on all 2 133 rows, so there was no pre-existing
typing to build on — which is the reason this layer exists at all.

### Authority beats recency

The rule that keeps a confident machine from overwriting a person:

```
  1  user_statement        Brady said it, or an explicit manual correction
  2  legacy_extracted
  3  secondary_summary
  4  assistant_inference
```

A newer assistant inference **never** supersedes an older user statement. `supersede` may only
auto-retire a value of equal or lower authority. When two current statements disagree, **both
remain current**, each carrying `conflicts_with`, and the disagreement is displayed rather than
settled. A statement's extraction timestamp is never presented as the event date.

A **plan is not a completion**: "I'll send Chris the packet" is stored as `attribute='plan'`,
and a later assistant turn claiming "sent" does not promote it. The **board remains the sole
authority on task state** — the migration never closes, reopens, recreates or edits a board
row, and where a user statement disagrees with live board state the dossier reports a
*discrepancy* instead of resolving it.

Board rows are canonical task state, but their titles can be Ace-generated, so board text is
labelled a **board record** and is never rendered as a direct user quotation.

### Identity: the rules that prevent invented people

1. **Exact normalized alias equality is the only resolver.** No fuzzy matching, no edit
   distance, no embeddings, no model calls on the resolve path — ever. `Sienna` and `Syanna`
   normalize differently and stay two entities.
2. **A name resolving to ≥2 active entities resolves to none.** The source is marked
   `ambiguous`, a review row records the candidates, and no link is written.
3. **A first-name-only alias never resolves on its own**, even when exactly one candidate is
   currently known — "only one Jordan so far" is a fact about today's data, not about the
   world.
4. **The idempotent import key is not the display name.** `ace_entities.import_key` is UNIQUE;
   `display_name` and `alias_norm` deliberately are not. Two people may legitimately share a
   name, and nothing may collapse them. A shared `alias_norm` raises a `name_collision` review
   row — a notice, not a merge.
5. **Fuzzy similarity may only ever produce a `merge_candidate` suggestion** for a human.
6. **Graph snapshots are candidate seeds only.** Importing `graph_cache` creates review rows —
   no entities, no relations. A seed becomes real only through an explicit human confirmation
   that records actor and reason in `ace_entity_audit`.
7. **Retracted links and rejected reviews are permanent tombstones.** Replay must not
   resurrect them.

Rule 6 is not theoretical caution. In the live graph cache, `PFI` and `GFI Legends` — both
organizations — are typed `person`, and the 48-node snapshot claims types (`deal`, `category`)
that are not in this layer's vocabulary at all. A model's opinion about identity is a
suggestion.

### Personhood needs context, not capitalization

The first migration prototype promoted any capitalized span seen in two sources and defaulted
its type to `person`. Against the real corpus that produced 283 "people" including
`Account Manager`, `Business Review`, `Call Armando`, `Build Team` and `Base Cost`, and typed
`Allianz Life`, `Ally Bank` and `Amazon Prime` as people. That is the old LLM-graph failure
reproduced deterministically, and it was rejected.

The replacement uses evidence rather than orthography:

- **Corpus case statistics.** One deterministic pass counts each token's lowercase versus
  capitalized occurrences. A token appearing lowercase frequently is a common word and cannot
  be a name token. "account", "business", "review", "call", "build", "team", "base", "cost"
  are eliminated *by the data*, not by a hand-maintained blocklist. No model, linear cost.
- **Common-word tokens are stripped from span edges**, so "Call Armando" becomes "Armando" and
  "Business Review" becomes nothing at all.
- **Person requires person context** — an explicit introduction in a user statement, or a
  person-context construction ("call X", "X said", "X's", "my ‹relation› X") across ≥2 distinct
  sources, at least one of which is a user turn or the profile. Board rows and legacy-extracted
  facts corroborate but cannot promote, precisely because board titles can be Ace-generated.
- **There is no person default.** A span with neither person nor org evidence becomes no entity
  of any type; it remains a review candidate with its claimed type recorded as unknown.

Unreviewed suggestions are expected and retained. Confidently wrong people are not.

### Assistant save-narrations are excluded knowledge

Many legacy facts are concatenated narrations ("Learning sweep complete. Saved two updates:
…") naming several people together. `chat._is_meta_fact` already keeps these out of live
context; this layer indexes them as assistant meta-history with an exclusion reason and
**never infers a relationship from co-mention inside one**. They are kept as raw source
references and are not split into invented verified facts. Where such a narration names a `.md`
filename, no claim is made that the document exists.

---

## 2. Contract and schema

### Tables (all new; rollback drops exactly these)

`ace_entities`, `ace_entity_aliases`, `ace_sources`, `ace_entity_links`, `ace_entity_facts`,
`ace_entity_relations`, `ace_entity_review`, `ace_entity_audit`, `ace_index_checkpoint`.

Created by `entities.ready()` — idempotent `CREATE TABLE IF NOT EXISTS`, in the style of
`ops.ready()`. Never added to `db._init_schema()`, so an un-migrated database behaves exactly
as it does today and the layer is inert until the backfill runs.

Full column definitions live in `ace2/backend/entities.py`, which is the authority. The
identity-critical constraints are worth restating because they encode the rules above:

```
ace_entities.import_key        UNIQUE   -- idempotency, NOT identity by name
ace_entities.display_name      no unique constraint, deliberately
ace_entity_aliases             UNIQUE(entity_id, alias_norm)
                               -- NOT unique on alias_norm alone: two people share "Chris"
ace_sources.source_id          PRIMARY KEY  -- "<corpus>:<native_id>", idempotent replay
ace_entity_links               UNIQUE(entity_id, source_id, relation)
ace_entity_review              UNIQUE(kind, subject_key)
```

### Identifier validation

```
source_id   ^(fact|turn|item|profile|summary):[A-Za-z0-9_-]{1,64}$
entity_id   ^(per|org|prj)_[a-f0-9]{12}$
```

Enforced in the store and again at the route. A source identifier can never carry a filesystem
path or a SQL identifier, and no caller input is interpolated into SQL — table names included.

### HTTP API

Every route below carries the same `Depends(require_auth)` as the rest of the app, read-only
ones included.

| Route | Purpose |
|---|---|
| `GET /entities` | search/list entities |
| `GET /entities/{id}` | full dossier |
| `GET /entities/counts` | honest totals |
| `GET /entities/review` | the review queue |
| `POST /entities/{id}/correct` | governed, audited correction |
| `POST /entities/review/{review_id}` | confirm / reject / dismiss a suggestion |
| `GET /sources/search` | global source search, reaches unassigned + ambiguous + excluded |
| `GET /graph?source=entities` | entity-backed graph, **zero model calls** |
| `GET /graph?source=legacy&refresh=1` | the old paid LLM rebuild, preserved behind a flag |

Correction ops (`rename`, `add_alias`, `remove_alias`, `confirm`, `reject`, `unlink`,
`link_item`, `merge`, `unmerge`, `set_fact_status`, `set_relation_status`) are
**non-destructive**: they set status or `retracted_at`, never DELETE, and never touch an
original row. Every op writes `ace_entity_audit`. An unknown op is a 400 with nothing written.

No credentials, keys, raw auth values or settings ever appear in a dossier, a search result or
the graph UI — `internal_metadata` sources are excluded by construction, with a redaction pass
as belt-and-braces.

### Retrieval contract

`entity_context.registry_block()` injects an **index** — names, types, ids and counts only,
never private facts — into both the typed and voice context, character-bounded, behind a 1.5 s
budget, omitted entirely on failure. Detail is pulled deliberately via the read-only
`lookup_entity` tool, which returns current facts (each labelled `[Brady said]` /
`[Ace inferred]` / `[legacy extracted memory]` with its date and source), dated history,
related entities, linked board items with **live** status, and explicit conflicts. Everything
is bounded before return; nothing concatenates the corpus.

Source excerpts are wrapped and prefixed as quoted records, not instructions. Every dossier
carries the standing note that **Brady's budget spreadsheet is the money authority**; nothing in
this release derives, sums, totals or infers a figure.

---

## 3. The two repaired defects

### Defect 1 — false "Outcome unconfirmed" on writes that succeeded

Three production turns changed exactly the intended row and all three replies said the outcome
was unconfirmed; one said confirmed and unconfirmed in the same breath.

The chain was honest at every link and wrong at the end: `tools._do_update_item` returned a
prose string, `ops.classify` can only classify prose as `REPORTED` ("claimed, nothing
verified"), `chat._OPS_MEANING` maps `REPORTED → OP_UNKNOWN`, and `guarded_reply` therefore
suppressed the true confirmation and appended a warning.

The repair does not trust prose. `db.update_item_verified` resolves the target, snapshots the
row, hands the write to the existing `update_item` (whose guards are **not** duplicated), reads
the row back in a fresh read, and compares the persisted values against the **requested** ones
via `db._expected_item_values` — which also asserts that omitted fields were preserved and that
`after.id` is the intended target. The resulting ladder:

| Evidence | Outcome |
|---|---|
| store refused | `FAILED_BEFORE_DISPATCH`, refusal text verbatim |
| accepted, row unreadable | `REPORTED` |
| accepted, persisted ≠ requested | `REPORTED` |
| verified, nothing moved | `COMPLETED` — "already satisfied … no task fields changed" |
| verified, fields moved | `COMPLETED`, naming only the fields that actually moved |

A diff alone is **not** evidence: something moving does not mean the requested thing moved.

The receipt verb is derived from the `before.status → after.status` transition, so a text edit
on an open row says **Updated**. The old code took the verb from the `status` argument, so
every edit passing `status='open'` — which is what an edit to an already-open row harmlessly
does — answered "Reopened", and Brady was told a renamed task had been brought back from the
dead. That is now structurally impossible.

The Drive fallback is capped at `REPORTED`: there is no saved row to read back there, and
claiming `COMPLETED` would reinstate the defect on the degraded path.

`capture_item` was **not** touched — it already returned a structured `ops.Outcome` and does
not have this defect.

**A hazard inside the fix, and its guard.** `_expected_item_values` independently re-derives
`update_item`'s normalization (`pin_due`, `canon_tags`, `.strip()`, the 300-character
`next_step` cap, `""`→`'active'`, "leaving WAITING drops the owner"). Two copies of one rule
set drift, and the drift fails in the worst direction: the oracle disagrees with the writer,
every ordinary edit falls to `REPORTED`, and the false-unknown warning returns — the live
defect reintroduced through its own fix. `tests/test_receipt_oracle_drift.py` runs the real
writer against real PostgreSQL across 17 edit shapes, one per re-implemented rule, and asserts
the oracle predicted the stored row exactly. It is mutation-tested: changing the oracle's
`next_step` cap from 300 to 200 makes it fail with a diagnostic naming both values.

### Defect 2 — stale failed cards blocking the phone board

On a real 390×844 browser, clicking a board row's Edit button timed out because an old failed
deep-dive card intercepted the pointer.

Two independent causes, both fixed:

- `syncTaskCards()` re-rendered **every** `failed` card from `/actions?limit=20` on each
  WebSocket open **and** each `visibilitychange`, while dismissal lived only in an in-memory
  map — so dismissal never survived either event and the stack grew without bound.
- `#task-layer` is `z-index: 70` against `#command-view`'s `60`, so cards legitimately sat on
  top of the open board.

The repair: persistent per-device dismissal keyed `task_id@version` (version =
`settled_at || updated_at || created_at`, so a genuinely new failure surfaces once and the old
one never returns); a terminal card may surface from `syncTaskCards` only when unseen,
undismissed and settled within 30 minutes; a visible-card cap with an overflow affordance; and
`body.panel-open #task-layer { z-index: 58 }` so cards drop behind an open board or graph.

Dismissal performs no fetch and starts no job. Nothing in this slice ever retries work.

### Notification preference

Three settings — **All activity / Results only (default) / Off** — stored per device in
`localStorage` (`ace.notify.mode`, `.dismissed`, `.seen`; both maps pruned to 200 entries /
30 days, every access wrapped so a storage failure degrades to in-memory).

Per-device is the deliberate choice: a popup is a property of the screen you are looking at,
not of the account; the phone and the laptop want different answers; and it needs no new
server state, no migration and no auth surface. Server-side Activity history is unchanged and
remains the record.

`needs_approval` **always** renders, in every mode including Off — Off suppresses optional
popups, never a mandatory approval gate and never the Activity history. Because "Results only"
hides in-progress popups, the **Stop** control is also available on live rows in Recent
activity, so cancelling never depends on a popup being visible.

---

## 4. Deployment, backfill and rollback

**Codex owns production execution.** Nothing below was run against Railway from this session.

```bash
# 0. Backup first. Codex has: ../backups/ace-sept21-memory-preupgrade.sql
#    sha256 f90e8a5af9c378f7d643041d36a3779868ba7d05ae7f91f3906efbeaeebefe34

# 1. Integrity baseline
python check_source_integrity.py

# 2. Dry run — THE DEFAULT. Writes nothing. Full report.
python -m ops.entity_backfill

# 3. Apply, explicitly
python -m ops.entity_backfill --apply

# 4. Originals must be byte-identical
python check_source_integrity.py

# 5. Replay must be a numeric no-op
python -m ops.entity_backfill --apply
python check_source_integrity.py

# Rollback — drops ONLY the new tables, after printing them
python -m ops.entity_backfill --rollback --yes-drop-entity-layer
```

The runner is dry-run by default, transactional per batch, resumable from
`ace_index_checkpoint`, and idempotent. It captures a row-count + aggregate content-hash
manifest per original table **before and after**, and any drift is a hard failure with a
non-zero exit.

The report states, by name: `indexed`, `candidates`, `entities_created`, `linked`,
`unresolved`, `collisions`, `excluded` (by reason) and `source_integrity`. Indexed + excluded +
unassigned + ambiguous reconcile arithmetically against the corpus total. The report never
prints a "fully organized" claim; it prints the unresolved count.

Rollback is removing the mapping layer. No original row has to be restored, because none was
changed.

---

## 5. Tests and results

Baseline before this work: **701 pytest passed, 106 subtests**.
After: **822 passed, 114 subtests.** Every number below was produced by running the command,
not by reading a report.

### Python

| Check | Result |
|---|---|
| `pytest tests -q` | **822 passed, 114 subtests** |
| `tests/entity_store_check.py` | PASS — disposable Postgres, 49 synthetic sources |
| `tests/entity_api_check.py` | **161 checks, 0 failures** |
| `tests/receipt_repair_check.py` | PASS — real tool → dispatch → receipt pipeline |
| `tests/test_receipt_outcome.py` | 21 unit tests |
| `tests/test_receipt_requested_values.py` | 3 regressions (Codex's reproductions) |
| `tests/test_receipt_oracle_drift.py` | 17 — oracle vs. real writer, mutation-tested |

### Browser (real Chromium, desktop 1440×900 and phone 390×844)

| Check | Result |
|---|---|
| `notification_cards_check.cjs` | **99 passed, 0 failed** |
| `graph_detail_ui.cjs` | **70 passed, 0 failed** |
| `release_one_ui.cjs` | 84 passed, 0 failed |
| `voice_actions_ui.cjs` | 46 passed, 0 failed |
| `voice_continuity_ui.cjs` | 19 passed, 0 failed |
| `phone_fit_ui.cjs` | 14 passed, 0 failed |
| board editor / completion receipt / follow-up visibility / more-menu / capture queue / request order | all PASS |

`voice_actions_ui.cjs` gained one line making it opt into `ace.notify.mode='all'`. It raises a
`working` task and waits for a card, which "Results only" now suppresses by design; every
assertion in that file is *about* the progress card, so the file has to ask for the mode that
shows them. No assertion was weakened. `notification_cards_check.cjs` is what covers the
default and Off.

### Migration, against the disposable restored copy of production

```
integrity baseline ........... all 8 original tables unchanged: True
dry run ...................... 33 entities previewed, nothing written
apply #1 ..................... 33 created {person 15, org 16, project 2}
integrity after apply ........ all 8 original tables unchanged: True
apply #2 ..................... entities_created 0, linked 0, entity_facts 0, review_new 0
integrity after replay ....... all 8 original tables unchanged: True
```

Reconciliation balances exactly: **indexed 993 + excluded 1874 + unassigned 4496 +
ambiguous 1286 = 8649**, against 8 649 scanned.

Populated layer: 33 entities, 1 284 active links across 993 linked sources, 178 entity facts
(plan 91, status 60, role 14, relationship 7, location 4, org 4), 1 030 open review rows, 0
relations (graph seeds create none by design). 31 of 33 entities carry at least one link; the
two that do not are given-name-only people created from explicit introductions, where a
first-name alias correctly never resolves.

Review queue, now priority-ranked by evidence strength rather than a flat constant:
`ambiguous_name` 26 and `name_collision` 1 at priority 1, `unpromoted_name` 400 spread across
`{1: 23, 2: 54, 3: 176, 4: 147}`, `unassigned_source` 555 at 5, `graph_seed` 48 at 6.
`ORDER BY priority` alone surfaces the names that matter most first.

Migration reports are saved outside the repository, at
`../memory-upgrade-sept21/entity-backfill-{dryrun,apply1,apply2}.private.json`. They contain
real names and must not be committed.

### Two bugs the tests caught that the reports did not

- **A cursor that silently skipped a third of the corpus.** `SELECT id::text … ORDER BY id`
  bound `id` to the *text* output alias, so pagination sorted lexicographically while the
  cursor predicate was numeric — dropping 958 of 2 133 facts, 958 of 4 154 turns and 957 of
  1 813 summaries while reporting a clean run. Fixed, with a per-corpus
  `scanned == rows_in_corpus` assertion that now fails the run loudly.
- **A stacking-context fix that could not work.** The specified
  `body.panel-open #task-layer { z-index: 58 }` was unachievable: `#app` is
  `position: relative; z-index: 2`, so panels inside it painted at body level 2 while the
  card layer sat at body level 70. No number inside `#app` could win. The card layer now
  lives inside `#app` so panels and cards share one stack. Separately the layer had no
  height bound, so a 614 px stack in a 476 px band drew off-screen — present in the DOM,
  reported visible, untouchable. Both are now asserted by bounding box, not by "a click
  resolved".

---

## 6. Coverage boundary — what is NOT indexed

Stated explicitly because a memory layer that overstates its reach is worse than one that
admits a gap. The PostgreSQL index covers `facts`, `turns`, `daybank_items`, `summaries` and
the profile. It does **not** cover:

- **Old Drive monthly `ace2` history files.** Backfilled into `turns` at cutover; any
  Drive-only content never backfilled is not in this index.
- **The shared Telegram conversation window** (`ace_conversation.json`, read-only continuity
  with the bot).
- **The recovered pre-wipe archive** (`ace_history_recovered.json`, 496 messages).
- **Uploaded attachments.** `capture_store.py` can create a `capture_sources` table, but that
  table **does not exist in the production database** — verified against the restored copy,
  whose only tables are `facts`, `turns`, `daybank_items`, `summaries`, `ace_tasks`,
  `ace_write_ops`, `ace_review`, `push_subs`. Indexing uploaded sources is out of this pass.

All four remain reachable through the **existing `recall` fallback**
(`memory_db._build_corpus`), which this release preserves and does not modify. No new paid
image or OCR calls are made during migration.

---

## 7. Unresolved review policy

Unresolved is a state with a queue, not a silence.

- A source that matches no entity is `unassigned`. A name matching several is `ambiguous`.
  Both stay indexed, counted and searchable. Neither is ever dropped.
- `ace_entity_review` holds `ambiguous_name`, `unassigned_source`, `merge_candidate`,
  `graph_seed`, `name_collision` and `unpromoted_name` rows, deduped by `(kind, subject_key)`
  and prioritized so the queue is actionable rather than exhaustive.
- **Every unresolved item is reachable from the UI.** Per-entity suggestions appear in the
  dossier; the global queue and global source search are reachable from the graph, because
  graph seeds create no entities and would otherwise have no entrypoint at all. A count that
  says "N unresolved" with no way to open it is not acceptable.
- Confirming or rejecting a suggestion goes through the governed correction API and is
  audited with actor and reason. A rejection is a permanent tombstone; replay will not
  resurrect it.
- The visual node cap is presentation only. Search and dossiers reach every record, and the
  graph header states how many are not drawn.

---

## 8. Reusing this for Nigel

The layer is deliberately generic: it knows about `person`, `org` and `project`, a source
index keyed by `"<corpus>:<native_id>"`, and an authority ordering. It knows nothing about
Brady, insurance, or PFI.

To reuse it:

- Take `entities.py`, `entity_index.py`, `entity_migrate.py` and `ops/entity_backfill.py`.
  I grepped these four for Ace-specific coupling: every occurrence of "Brady", "PFI", "GFI" or
  "insurance" is in a comment or docstring explaining why a rule exists — except **one real
  code dependency, `entities.MONEY_NOTE`**, which hardcodes Brady's budget spreadsheet as the
  money authority. That constant must be re-pointed or removed (see below).
  The other adaptation points are the corpus readers in `entity_index.read_corpus` and the
  `classify_source` mapping, both small and explicitly table-driven.
- **Copy no data.** Entities, aliases, sources, links and review rows are all derived from the
  target system's own corpus. There is no seed list, no shared dictionary and no exported
  model. Running the backfill against a different database produces a different population
  with no trace of this one. The corpus case statistics are computed per corpus, so they adapt
  rather than carry Brady's vocabulary across.
- **Assume no financial compliance.** This layer stores quoted statements with provenance; it
  performs no calculation, reconciliation, totalling or reporting, and it is not a book of
  record. `entities.MONEY_NOTE` must be re-pointed at whatever the authoritative financial
  source is in the new deployment, or removed along with any implication that the layer has an
  opinion about money. Nothing here has been assessed against any financial, regulatory or
  record-keeping standard, and nothing here should be treated as evidence in one.
- The identity rules — no fuzzy merging, no first-name resolution, no promotion without
  context, human confirmation for model suggestions — are the valuable part and should be
  carried over intact. They are what stops a memory layer from inventing people.

---

## 9. Limitations — stated plainly

- **This is not "perfect memory" and the corpus is not fully organized.** A large fraction of
  mentions remain unresolved by design, because resolving them would mean guessing.
- A search index is **not** a verified person dossier. Confirmed links and unreviewed
  suggestions are stored and displayed separately, and only a human can promote one.
- Graph seeds create no entities, so the entity graph starts **sparse**. That is the honest
  state, not a failure to populate.
- No live production turn was re-run from this session; the three-turn live receipt
  reproduction is Codex's step.
- The daily paid-capability cap has a latent timezone coupling (`tasks.reserve_daily` compares
  `created_at >= %s::date`, which resolves in the DB session timezone, against a day string
  derived in the process timezone). Production is consistent today because Railway runs both
  in UTC. Out of scope for this release; flagged, not fixed.
