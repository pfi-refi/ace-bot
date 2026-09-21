# Ace board reliability repair — September 21, 2026

Release candidate v2.0.5, built on 820b6d7 (v2.0.4). No schema migration, board backfill, row recreation, or design change. Existing voice and background-work repairs remain included.

## Changes

- Actions can be saved as Waiting. The editor previously offered it while the route refused it.
- Ready/Active is explicit: omitted state preserves the old state; the editor's empty state stores `active`. Storing NULL would let old wording derive Waiting again.
- Leaving Waiting clears its owner unless an owner was explicitly supplied. Ace's tool treats empty optional state/owner strings as omitted; an explicit `state=active` is required to unpark through that tool.
- Settled is for reference records. Validation is shared by the HTTP and tool paths. Unknown kind is refused without a write, using an accurate retry message. This deliberately rejects the reviewer's suggestion to allow writes after a failed lookup.
- Non-completion refusal reasons now reach the editor. HTTP errors and expired sessions produce visible failures and retain typed edits.
- The editor preserves the waiting-on draft and refreshes board/Today from the same fetched payload. Stored `active` explicitly maps to the Ready/Active option.
- Unsupported edits on the old Drive fallback are refused instead of reported as saved.
- Service-worker cache version updated to deliver the new interface assets.

## Independent review and tests

Claude Code used two implementation agents and a separate reviewer, followed by bounded corrections. Codex independently found and reproduced the NULL-state re-derivation defect, exercised actual browser flows, hardened HTTP failures, and kept unknown-kind handling conservative.

Verified locally with synthetic rows and disposable PostgreSQL, without production writes or model API calls:

- 701 pytest tests plus 106 subtests.
- `board_repair_check.py`: actual HTTP/tool paths and fresh database readbacks for state transitions, omitted fields, text/date/tag edits, refused writes, parent preservation and Today consistency.
- Existing board integration, release-one, release-candidate checks.
- JavaScript editor, completion receipt and follow-up visibility checks.
- 84 real-app browser assertions: desktop/phone, completion guards, editing and view consistency.
- Additional real phone-sized browser flow: Waiting + owner save, Ready reset, text edit, reload persistence, completion from Today.

Claude's initial report claimed JavaScript was unavailable because its shell commands were denied. That was a session permission limitation. Codex executed the JavaScript/browser checks with the installed bundled Node runtime.

Private execution evidence is in `work/review-sept21/`. No live voice or paid conversation test was performed; this release does not claim broader conversational acceptance.

## Boundaries

This repairs board operations. It does not build the planned Reminders-style lists/subtasks/Scheduled redesign or change personal/client context storage. Existing items and history remain intact.

The existing Remove action archives an item (`dropped`) separately from completion and retains its existing behavior. Waiting/reference completion still requires explicit confirmation. Once a user explicitly sets a status, it takes precedence over guesses from old text.

A fresh database dump was captured before release work. Release/deployment verification will be recorded below when performed.
