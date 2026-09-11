# Reliability candidate — September 7, 2026

Base: 1201fadae2a5fad891624e9a2656a8d4083ebc20. This candidate is NOT deployed.

## Behavior changes

- Outward/destructive tool requests are proposals in More → Review actions. Natural-language approval and model `confirmed` flags cannot execute them. The authenticated UI claims the stored exact payload atomically. Expiry is 15 minutes. Reject unwanted/old versions. After an uncertain result, inspect the destination; replay is blocked.
- Public static files use an explicit allowlist; backend, configuration, readme and requirements paths return 404.
- Only unchanged text/date/parent repeats are silent duplicate no-ops. Similar wording with new detail returns an explicit review-needed result. Amount/date/parent distinctions apply before duplicate shortcuts. Text-only completion requires exact text; otherwise use a deliberately selected ID. Existing direct editing/check-off remains.
- More → Saved week shows a planning notebook and allows corrections without a model call. Planning conversations and full drafts are journaled for recovery; later sessions receive draft context. Interrupted draft text is labeled incomplete. This is NOT a transactional calendar scheduler and does not guarantee a model follows every correction.
- Bills use structured Google Sheets values, the named Bills & Expenses tab and header mapping. Explicit No/false is not treated as paid. Month/day deadlines in Notes are included and labeled. Existing Google credentials must have Sheets access; failure is explicit, never a board-money fallback.
- Prepared ops/bridge_worker.py preflights the configured Claude CLI before claiming jobs. NOT installed on the Mac. Server-side claim/fallback recovery remains separate unfinished work.

## Validation

Python 3.12 isolated environment with ace2/backend/requirements.txt:
`python -m unittest discover -s tests -v` — 24 passing tests.

Optional local integration dependency `pgserver==0.1.4`:
`python tests/postgres_check.py` — disposable local Postgres; real schema, concurrent approval claims, expiry/rejection/replay, notebook writes/reads, distinct board obligations and completion.

Browser verification uses synthetic intercepted responses, no production authentication: Review displays exact payload, Reject dispatches once, Saved week opens, 390px viewport has no horizontal overflow, no script errors. Full app startup/import and Python/JS syntax were checked. The mocked stream test starts a new voice conversation and verifies that saved draft text reaches the model context.

No live writes, paid model calls, real ElevenLabs sessions, emails, calendar actions, client changes or deployments occurred. Browser test uses the actual new UI but mocks the legacy app controller; it is not a full production PWA session test.

## Limitations to resolve/observe

- Legacy tool executors return prose, so Review preserves their result and labels non-explicit-failures unknown; verified structured success receipts remain future work.
- Pending reviews are account-wide for this single-user app. They are not a multiuser permission system. Changed proposals are separate entries; user rejects old entries explicitly.
- Board matching is more conservative. Some paraphrases require review/update instead of an automatic merge. Concurrent board add idempotency and full mutation/undo history are not implemented.
- The notebook is heuristic capture, not a structured versioned plan/execution engine. It returns up to 60 entries from the last seven days and caps model context; older records remain in the database. Additional context adds input tokens and needs latency measurement in a permitted voice test.
- Direct Sheets authorization is not verified against deployed credentials. Test that read-only connection after deployment; do not declare the financial brief repaired end-to-end without a permitted brief evaluation.
- The source repo's public visibility is unchanged.
- There is no demonstrated dollar savings yet. Keep model and ElevenLabs settings unchanged until usage/billing evidence supports changes.

## Deployment and rollback

Brady required review before deployment; do not push/merge to the auto-deploy branch until he approves. Compare against the then-current production HEAD to avoid overwriting Claude's concurrent work.

Before deployment: verify a recent recoverable database backup through the hosting account. New ace_review table is additive; existing tables/rows are not migrated or deleted by this candidate. The local baseline and worktree are not substitutes for a database backup.

After approval: deploy candidate, check health version v2.0.1-review-candidate; unauth backend paths must be 404; legitimate shell/manifest/icons load; unauth /reviews and /plan/draft must be denied. Authenticate normally, confirm new UI, save a harmless draft, and test read-only Sheets access. Real AI/voice calls need separately permitted small acceptance testing; no real email/delete test is necessary.

Rollback to the verified prior deployment if smoke checks fail. Keep ace_review records for recovery. Returning to the baseline also restores the old approval and static-source defects, so rollback requires awareness of those unresolved risks.

The local bridge worker is a separate install with its own file backup and CLI authentication verification. Do not simply re-enable it after login while assuming server claims recover correctly.


## Upgrade-awareness check for every release

Update `ace2/CHANGELOG.md` when user-facing behavior changes. Keep newest entries first, concise, and include limitations and verification boundaries. The most recent five complete entries must fit within6000 characters; do not put planned work among shipped abilities. Both voice and typed context must receive the shared upgrade block. Run `tests/test_upgrade_awareness.py` through unittest discovery before release. Notes do not prove credentials or provider health.
