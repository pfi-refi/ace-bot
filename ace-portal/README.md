# ACE Portal — PFI Command Center

Brady McGraw's desktop interface for **Ace**, his Claude-powered AI assistant. This
replaces Telegram as the daily driver. Same brain, same memory, same conversation
history — Ace is stateful across both surfaces because both read/write the same
Google Drive files.

Electric command-center aesthetic: black background, electric lime `#39FF14`,
Matrix digital rain, and a neural-network particle orb.

```
┌──────────────────────────────────────────────────────────┐
│  ACE ◈ PFI COMMAND CENTER      [ 07:29:17 PM ]  JUL 15    │
├──────────────┬───────────────────────────┬───────────────┤
│  SCHEDULE     │      ◍ ORB  · ACE ·        │  INTEL FEED    │
│  Today        │      [ IDLE ]              │  (signals)     │
│  Upcoming     │      chat transcript       │  ───────────   │
│               │      [ message Ace… ] 🎙 ➤ │  TASKS         │
├──────────────┴───────────────────────────┴───────────────┤
│  ACE v18.17  ◈ Calendar ◈ Gmail ◈ Tasks ◈ Drive   CLE °F  │
└──────────────────────────────────────────────────────────┘
```

## Architecture

- **Frontend** — vanilla HTML/CSS/JS, no build step (`index.html`, `styles.css`, `app.js`).
  Matrix rain, particle orb, live clock, streaming chat, Web Speech voice in/out,
  calendar / tasks / weather / memory panels, quick actions.
- **Backend** — FastAPI (`backend/`). Google API functions are **ported directly
  from `ace-bot/ace-bot/bot.py`** — not rewritten. Ace's action-tag loop
  (`[CREATE_EVENT:]`, `[ADD_TASK:]`, …) is preserved exactly; the only adaptation
  is streaming the reply over a WebSocket with tag text filtered out live.
- **Real-time** — `WS /ws/chat` streams Ace's tokens; `POST /chat` is an HTTP fallback.

### Backend modules
| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, WebSocket, auth, static serving |
| `ace_chat.py` | Anthropic call + action-tag loop (ported from `_process_text`) |
| `system_prompt.py` | Ace's system prompt (ported from bot.py `SYSTEM_PROMPT`) |
| `calendar_api.py` | Google Calendar r/w — **`parse_time_flexible()` ported verbatim** |
| `tasks_api.py` | Google Tasks + Gmail + Drive helpers |
| `memory.py` | `ace_memory.json` + `ace_conversation.json` on Drive (read/write only) |
| `weather.py` | OpenWeatherMap proxy (key stays server-side) |
| `google_client.py` | Shared OAuth creds + constants |

## Endpoints
```
POST /auth      → exchange PORTAL_PASSWORD for a session token
GET  /health    → version + uptime
GET  /calendar  → upcoming events (structured, 7 days)
GET  /tasks     → open Google Tasks (structured)
GET  /memory    → ace_memory.json contents (source of truth)
POST /memory    → merge new fact(s) into ace_memory.json
GET  /weather   → Cleveland weather
POST /chat      → non-streaming Ace turn
WS   /ws/chat   → streaming Ace turn
```

## Source of truth
`ace_memory.json` on Google Drive is the **only** source of PFI business data.
**No PFI metrics are hardcoded anywhere in this portal.** Any business number the
UI shows comes from `GET /memory`. Ace pulls current figures from memory exactly
as the Telegram bot does.

## Environment variables
Shared with the `ace-bot` service on Railway — **do not create duplicates**:
- `ANTHROPIC_API_KEY`, `GOOGLE_TOKEN_JSON`, `GOOGLE_CREDENTIALS_JSON`

New for the portal:
- `OPENWEATHER_API_KEY` — free key from openweathermap.org (Cleveland weather)
- `PORTAL_PASSWORD` — single-user access code; leave blank to run open on Railway's private network

Optional: `ACE_MODEL` (default `claude-opus-4-8`, matching the live bot),
`ACE_MAX_TOKENS` (default `900`), `CORS_ORIGINS`, `PORTAL_TOKEN_SECRET`.

See `.env.example`. Never commit real secrets.

## Run locally
```bash
cd ace-portal
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
export ANTHROPIC_API_KEY=... GOOGLE_TOKEN_JSON=... OPENWEATHER_API_KEY=... PORTAL_PASSWORD=...
uvicorn backend.main:app --reload --port 8099
# open http://127.0.0.1:8099
```

## Deploy (Railway)
New service named **`ace-portal`** in the same Railway project as the bot.
Nixpacks builds from `nixpacks.toml`; start command:
```
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```
Point the service at the shared env vars, add `OPENWEATHER_API_KEY` and
`PORTAL_PASSWORD`, and deploy. Railway provides TLS automatically.

## Security notes
- All API keys stay server-side; the browser only ever holds a session token.
- `calendar_id='pfi@platinumfortuneimpact.com'` is explicit on every calendar write.
- Drive data files are **read/write only** — never deleted or restructured.
- `parse_time_flexible()` is preserved verbatim from bot.py.

## Phase 2 (not yet built)
`manifest.json` is stubbed for PWA install. Phase 2 adds a service worker, a
single-column mobile layout, and "Hey Ace" wake-word so the portal can replace
Telegram on iPhone.
