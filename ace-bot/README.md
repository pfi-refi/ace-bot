# Ace — Brady's Telegram Business Advisor Bot

Ace is a personal AI bot that sends Brady a weekday morning briefing at 9:30 AM ET
and responds to messages throughout the day with context-aware business advice.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot — message handler, scheduler, commands |
| `system_prompt.py` | Brady's business context + Ace's personality |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway process definition |
| `.env.example` | Environment variable template |

---

## Step 1 — Get Your Telegram Chat ID

Before deploying, you need your personal Telegram chat ID (this is how Ace knows it's you).

1. Open Telegram and search for `@userinfobot`
2. Start a chat with it and send `/start`
3. It will reply with your numeric User ID — copy that number
4. That's your `BRADY_CHAT_ID`

---

## Step 2 — Deploy to Railway

### Option A: Deploy from GitHub (recommended)

1. Push these files to a GitHub repo (public or private)
2. Go to [railway.app](https://railway.app) and sign in
3. Click **New Project** → **Deploy from GitHub repo**
4. Select your repo
5. Railway will detect the `Procfile` and set up a **worker** service automatically

### Option B: Deploy directly (no GitHub)

1. Go to [railway.app](https://railway.app) and sign in
2. Click **New Project** → **Empty Project**
3. Click **+ New Service** → **GitHub Repo** or drag-and-drop your project folder

---

## Step 3 — Set Environment Variables

In your Railway project, go to your service → **Variables** tab → add these three:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `BRADY_CHAT_ID` | Your numeric Telegram chat ID (from Step 1) |

**How to get your Telegram bot token:**
1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow prompts
3. Name: `Ace` / Username: `ace_daily_bot`
4. BotFather gives you a token — that's `TELEGRAM_BOT_TOKEN`

---

## Step 4 — Deploy

1. After setting environment variables, Railway will auto-deploy
2. Go to the **Deployments** tab to watch logs
3. You should see: `Starting Ace bot…` and `Scheduler started — morning brief runs Mon–Fri 9:30 AM ET.`

---

## Step 5 — Test It

1. Open Telegram and find your bot (`@ace_daily_bot`)
2. Send `/start` — Ace should respond with a welcome message
3. Send `/brief` — triggers a manual morning briefing to confirm Claude is connected
4. Send any message — Ace responds with business advice

---

## Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message + command list |
| `/brief` | Trigger a manual morning brief right now |
| `/reset` | Clear conversation history (fresh context) |

---

## Schedule

Ace sends a morning briefing automatically every **Monday–Friday at 9:30 AM Eastern Time**.

The scheduler uses `US/Eastern` timezone via APScheduler — it handles daylight saving automatically.

---

## Security

The bot **only responds to your chat ID** (`BRADY_CHAT_ID`). Any messages from other
Telegram users are silently ignored and logged. Your bot token and API key never touch
the code — they live only in Railway's environment variables.

---

## Troubleshooting

**Bot not responding:**
- Check Railway logs for errors
- Verify all three environment variables are set correctly
- Make sure the worker service is running (not a web service)

**Morning brief not arriving:**
- Confirm Railway's worker is running (not sleeping — Railway workers stay on 24/7)
- Check that `BRADY_CHAT_ID` is set to your numeric ID, not your username
- Verify timezone: Railway workers run in UTC internally, but APScheduler uses `US/Eastern`

**Claude errors:**
- Verify `ANTHROPIC_API_KEY` is valid and has credits
- Check Railway logs for `APIError` messages

---

## Updating the Bot

To update Brady's context, priorities, or Ace's personality: edit `system_prompt.py`,
push to GitHub, and Railway auto-deploys.

To add new commands or behaviors: edit `bot.py` and redeploy.
