# Ace 2.0 — Build Dossier: The Most Advanced Version

> Researched across 4 domains, fact-checked by 2 adversarial agents (6 agents, ~480K tokens, 2026-07-26).
> Designed companion: https://claude.ai/code/artifact/9f7680fc-9922-4f59-af04-a9d4082b26b4
> Tags: **Quick Win** (days) · **High Value** (1–2 wks) · **Big Bet** (flagship).

---

## 01 · Where Ace is today

**Already built & live:** unified voice+chat on one thread; compounding tiered/bi-temporal memory (reconcile: skip/supersede/add); background learning+triage sweep (Opus) that auto-files facts + routes to-dos; Plan-my-Week ritual + one-tap button; Google Calendar/Tasks/Gmail/Drive/Docs/Sheets; confirm-gates on send/delete. 17 native tools, ~66 durable facts, Opus deep brain + Haiku live voice.

**The gaps = the roadmap:** retrieval is keyword-ish (no semantic layer — #1 gap); interface has no graph/canvas/command-palette/live-views; proactivity is timer-only (no event triggers, no phone push); reach is PWA-only (no telephony); no photo/PDF/voice-memo/call-transcript capture; facts are flat (no people↔deals↔agents graph).

## 02 · Why Ace can beat the big products (the moat)
1. **Ownership** — self-hosted on his Railway/Postgres, Anthropic-only. Total personal memory that never leaves his infra / never trains a vendor model. (Limitless — the best ambient-memory product — was absorbed into Meta Dec 2025.)
2. **Fusion** — one brain that knows his calendar AND his 18 agents/carriers/pipeline/refis/recruiting. No per-seat SaaS crosses horizontal chief-of-staff × vertical base-shop CRM.
3. **Economics** — matching the stack piecemeal = $150–250+/user/mo and caps customization. Ace = raw API cost, whole shop, fully custom.

## 03 · The build — four pillars

### Pillar I — The Interface (Obsidian-grade command center)
Rendering backbone = declarative-JSON card engine (pattern behind Google's open **A2UI** project + CopilotKit **AG-UI**); fixed component catalog Claude arranges, not code. Buildable in the zero-build vanilla PWA.
- **Command palette (⌘K)** — search whole book + fire any action. S · Quick Win. (hand-roll vanilla, not React)
- **Generative JSON cards** — Claude returns a spec, tiny renderer hydrates. Platform move; graph/views/kanban ride on it. M · High Value.
- **Live knowledge graph** — deals/agents/prospects/carriers as nodes; orphaned leads, super-connectors. M–L · Big Bet. (force-graph, MIT, CDN)
- **Typed objects + queryable views** — Deal/Agent/Prospect/Policy objects; Table/Board/Calendar views on command (Obsidian Bases pattern). M · High Value.
- **Ambient layer** — Now/Next strip, orb-as-status, focus mode. S · Quick Win.
- **Weekly canvas + memory timeline** — spatial board (JSON Canvas) + bi-temporal scrubber ("what did we know in March?"). L · Big Bet.

### Pillar II — The Brain (memory & retrieval)
Build on his own Postgres; mine Zep/Graphiti, mem0, Letta for ideas he mostly already implements.
- **Hybrid retrieval** — pgvector + tsvector + pg_trgm → RRF → rerank. Foundation everything draws on. M · **Do First**.
- **Contextual Retrieval** — Claude writes per-chunk context before embedding; Anthropic measured up to −67% failed retrievals for ~$1/M tokens. S–M · High Value. *(verified)*
- **Entity-relationship graph** — people↔deals↔tasks on his bi-temporal columns; plain Postgres edges + recursive queries (no Neo4j). Multi-hop answers. M · High Value.
- **Self-improving sweep** — reflect on misses + distill playbooks (sleep-time compute). M · High Value.
- **Anthropic memory tool + context editing** — +39% perf / 84% fewer tokens on long tasks. S–M · Quick Win. *(verified)*
- **Corpus-wide summaries (GraphRAG)** — per-agent/deal roll-ups for "common blocker across stalled deals." M · High Value.

### Pillar III — Reach & Capture (everywhere, ingests anything)
Every integration ships a verified first-party/reputable MCP he already speaks.
- **Call & text (Twilio Labs MCP)** — text leads/downline, reminders, reach him by SMS. M · High Value.
- **Voice-memo auto-debrief (ElevenLabs Scribe)** — record→transcribe→extract→route. Already on ElevenLabs. S–M · Quick Win. *(standout)*
- **Call/meeting transcription (Fireflies/Granola MCP)** — calls → searchable memory + pipeline. S · High Value. (one already wired in env)
- **Photos/PDFs/docs** — feed multimodal Opus directly; parser only for high-volume tables. S · Quick Win.
- **CRM (GoHighLevel official MCP)** — if PFI runs on GHL, highest-leverage single integration. S–M · High Value.
- **E-sign (DocuSign MCP) + property data (ATTOM MCP)** — route paperwork; AVM/comps on new address. S–M · High Value.
- **Speed-to-lead auto-response** — ack + qualify new lead <60s, 24/7. M · High Value. (faster = far better; specific multiplier stats are marketing estimates, not fact)
- **Write in your voice** — style corpus + few-shot Claude; no new model. S–M · High Value.

### Pillar IV — Proactivity & Autonomy (he comes to you)
- **Daily brief + phone push** — morning game-plan + EOD recap as cards, PWA Web Push (iOS 16.4+), 👍/👎 tuning. M · High Value.
- **Ambient event triggers** — watch Gmail/Calendar, surface when it matters; rubric for interrupt vs. silent. M · High Value.
- **Named scheduled agents** — sweep → defined jobs ("Mon 6am: pipeline health"; "on new lead: qualify+draft"). M · High Value.
- **See & edit his memory** — viewer/editor over Postgres brain (trust). S · Quick Win.
- **Background deal-packaging** — parallel workers pull docs/check tasks/draft/flag gaps on Opus. Voice stays Haiku. L · Big Bet.
- **Portal & phone autonomy** — computer-use for carrier/MLS portals; AI phone agent answers + books. L · Big Bet.

## 04 · The sequence
- **Phase 1 (this week / quick wins):** hybrid retrieval foundation · command palette + ambient layer · daily brief + phone push · voice-memo debrief + photo/PDF capture · see/edit memory + memory tool.
- **Phase 2 (high value):** generative JSON card engine · typed objects + queryable views · entity graph + self-improving sweep · Twilio + speed-to-lead · CRM/DocuSign/ATTOM + transcription · write-in-voice + named scheduled agents.
- **Phase 3 (big bets):** live knowledge graph · weekly canvas + memory timeline · background multi-agent deal-packaging · portal computer-use + AI phone agent · ambient meeting capture.

## 05 · Fact-check (you said re-check all of it)
Two adversarial agents re-verified every load-bearing claim against primary sources. Changes made before anything reached the plan:
- **DROPPED** "text-to-hydration" (fabricated term; real anchors A2UI/AG-UI verified).
- **CORRECTED** ChatGPT "Pulse" retired mid-2026 (idea lives on as scheduled briefs); Follow Up Boss "AI Copilot/Smart Plans/multi-line dialer" were wrong names; A2UI dates (Dec 2025, v0.9) and Agent Skills (Oct 2025).
- **LABELED** all speed-to-lead stats as marketing estimates, not controlled studies (kept only the direction).
- **FLAGGED** Google Maps reference MCP is archived (wrap the Maps API directly instead).
- **Surprise in our favor:** Granola, Fathom, Buffer, Make, QuickBooks all ship real first-party MCPs.

Every tool named was verified to exist and do what's claimed.
