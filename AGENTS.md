# AGENTS.md — ace-bot repository

Guidance for coding agents (Codex, Claude Code, others) working in this repo.

## What lives here

| Path | What it is | Touch it? |
|------|------------|-----------|
| `nigel/` | **NIGEL StarCloud prototype** — static HTML/CSS/JS, fictional data, simulated AI. Current focus. | Yes. See `nigel/AGENTS.md`. |
| `docs/HANDOFF.md` | Running handoff for the NIGEL prototype: what changed, how to open, what was tested, limits. | Update it when you change `nigel/`. |
| `tests/nigel_ui_check.cjs` | Playwright walkthrough of the prototype at desktop and phone sizes. | Keep it green; extend it when you add flows. |
| `ace2/`, `ace-portal/`, `ace-bot/`, `bot.py`, `system_prompt.py`, `ops/`, other `tests/` | **Ace**, Brady's production Telegram/voice assistant. | **Not for NIGEL work.** Do not modify, deploy, or run against live services unless the task is explicitly about Ace. |

## Ground rules for NIGEL work

- Fictional data only. Every name, household, carrier, balance and reference number in `nigel/data.js` is invented; keep it that way.
- AI and submissions are simulated on the page. Do not call any model API, do not connect live systems, do not deploy, do not spend API credits.
- Work on the branch you were given. Commit with clear messages. Do not push to `main`.
- Ace stays untouched.

## Quick start

```
open nigel/index.html                       # no build step, file:// works
python3 -m http.server -d nigel 8080        # or serve it
node tests/nigel_ui_check.cjs               # Playwright, needs Chromium
```

Details, design intent and the upgrade list: `nigel/AGENTS.md`. History and test log: `docs/HANDOFF.md`.
