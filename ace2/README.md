# ACE 2.0 — PFI Command Center

Your personal Claude: a JARVIS-style command center that talks, sees your day,
and **takes real action** — books calendar events, adds/completes tasks, sends
and drafts email, searches Drive, and remembers what matters. Same brain and
memory as the Telegram bot; the bot stays your daily driver until this fully
replaces it.

Built with the Anthropic **Tool Runner** architecture (real tool use, no legacy
text-tags), self-hosted on FastAPI. `claude-opus-4-8` with adaptive thinking.

---

## Deploy (≈5 minutes, in Railway)

Ace 2.0 is a **new Railway service** on the existing `pfi-refi/ace-bot` repo.
It does NOT replace the portal or bot — it runs alongside them.

1. **Railway → New Service → Deploy from the `pfi-refi/ace-bot` repo.**
2. **Settings → Root Directory → `ace2`** (this is the important one).
3. **Variables** — add these. The first three are the *same values* your bot/
   portal already use (the Google token's scopes already cover everything):

   | Variable | Value |
   |---|---|
   | `GOOGLE_TOKEN_JSON` | same as the bot |
   | `ANTHROPIC_API_KEY` | same as the bot |
   | `OPENWEATHER_API_KEY` | same as the bot |
   | `ACE2_PASSWORD` | a new access code you choose (unset = no login) |
   | `ELEVENLABS_API_KEY` | your ElevenLabs key |
   | `ELEVENLABS_VOICE_ID` | the **ACE** voice id — see below |
   | `ELEVENLABS_MODEL_ID` | `eleven_flash_v2_5` (optional; this is the default) |

4. Deploy. Visit the service URL, enter `ACE2_PASSWORD`, and Ace is live.

That's it. Everything degrades gracefully if a variable is missing (no key →
that feature just shows "unavailable", nothing crashes).

### The one thing to confirm: the voice id

The **ACE** voice is already designed and saved in your ElevenLabs "My Voices".
Grab its id: **ElevenLabs → Voices → My Voices → ACE → ⋮ (More actions) →
Copy voice ID**, and paste it into `ELEVENLABS_VOICE_ID`. (Two candidate ids were
captured during the build — `KQDh5IkRcTsPOoUtwmVx` or `oATdOqNYCo0vTdwE77aN` —
but copying it fresh from that menu is the sure way.)

If the voice id is wrong or unset, Ace still talks — it falls back to the
browser's built-in voice, so nothing breaks; it just won't be *his* voice yet.

---

## Using it

- **Type or hold the mic** to talk to Ace. He replies in text and (if voice is
  on) speaks in his own voice — the orb pulses to his words.
- **Voice toggle** (speaker icon, right of the input) turns spoken replies on/off.
- **Layers** — Schedule, Weather, Intel, Tasks each collapse with the `–` in
  their header (remembered across sessions). MEMORY and HISTORY open overlays.
- **Actions happen for real.** "Book a call with Nina tomorrow at 2", "add a
  task to send the term sheet", "draft an email to the lender" — Ace does it and
  a `◈` tool pill shows the action firing. `send_email` only sends when you
  explicitly say send; otherwise he drafts.

### Layout direction

Default is **Command Rail** (Direction A). To try the others, open the browser
console and run `aceSetDirection('halo')` or `aceSetDirection('focus')` — it
persists. Tell me which you want and I'll make it the default.

---

## Later: realtime "talk over him" voice

For full-duplex JARVIS (barge-in, no push-to-talk), point an **ElevenLabs Agent**
at Ace's custom-LLM endpoint: `POST /v1/chat/completions` (OpenAI-compatible,
already built). Set `ACE2_LLM_KEY` and give the Agent that bearer token + the URL.
The Agent handles mic/turn-taking/STT/TTS; Ace's real brain (tools and all) is
the LLM. Note: this may need a higher ElevenLabs plan tier.

---

## Run locally

```
cd ace2
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
# set env vars (or leave unset for graceful degradation / no login)
uvicorn backend.main:app --reload --port 8000
```

Open http://localhost:8000.

---

## Architecture notes

- `backend/brain.py` — shared Google Drive brain. `ace_memory.json` is read+write
  (what makes 2.0 the *same* Ace). `ace_conversation.json` is **read-only** — 2.0
  never writes it, which is what keeps the Telegram bot safe. There is
  deliberately no `write_conversation_history()`.
- `backend/tools.py` — the action tools + `save_memory`.
- `backend/chat.py` — streaming tool-use loop; emits the `◈` tool events.
- `backend/history.py` — 2.0's own timestamped, append-only history.
- `backend/voice.py` — ElevenLabs TTS proxy (key server-side).
- `backend/main.py` — routes + the OpenAI-compatible voice-brain adapter.
- `index.html` / `styles.css` / `app.js` — the HUD (vanilla, zero build).
- `sw.js` / `manifest.json` — installable PWA (bump `CACHE_VERSION` in sw.js on
  any shell change).
