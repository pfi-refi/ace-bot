# AGENTS.md — NIGEL StarCloud prototype

You are upgrading a working, rough-draft prototype. Read this file, then
`../docs/HANDOFF.md`, then look at `reference/starcloud-reference.png`
before changing anything.

## The brief in one paragraph

NIGEL is Brady's "intelligence in service of a greater you" for Praxis
Technologies, a financial-advice practice. The StarCloud is the home screen:
an edge-to-edge blue / ice-white galaxy on near-black with a central light
column, floating system labels, a restrained text-only left sidebar, a date /
time / quote block top right, and a centred "Ask NIGEL..." bar at the bottom.
It should feel alive and cinematic and play like a game map you explore,
while remaining a serious work tool underneath. Amber means something needs
attention. Navigation is StarCloud → Systems → Sectors → Records.

## What exists

| File | Role |
|------|------|
| `index.html` | Shell: canvas, labels layer, sidebar, corners, HUD, drawer (`#stage`), conversation bar, modal. Loads Jost from Google Fonts with a system fallback. |
| `styles.css` | Single dark visual world. Tokens at the top. Sections: labels, sidebar, corners, drawer/panels, attention, buttons, record detail, satellites/HUD/tip, conversation, modal, responsive (≤860px = phone), ambient motion. |
| `starcloud.js` | Canvas renderer. `buildBase()` pre-renders the static field once per viewport (haze, arcs, dendrites, streaks, stars, column, starburst, node blooms, vignette). `initLive()` + `frame()` draw the moving layer each frame (sway, dust, comets, sparks, core, HUD rings, ring pulses, shooting star, node orbits, satellites, reticles, cursor). Input: drag / wheel / pinch / keys. Public API at the bottom (`setNodes`, `focus`, `reset`, `setInset`, `setSatellites`, `onCamera`, `ping`, …). |
| `app.js` | Hash router, views (StarCloud, Today, My Office, Systems, Sectors, Records, Record, Activity, Settings), breadcrumb, labels + satellites, HUD + hover tip, the simulated case workflow (memo → illustration → submission → issue), review actions, the simulated conversation, sessionStorage persistence. |
| `data.js` | Nine systems with sectors, four fictional households, three reviews (2 AUM under Investments, 1 Insurance under Servicing), seed activity. |
| `reference/starcloud-reference.png` | The visual target. Compare against it. |
| `../tests/nigel_ui_check.cjs` | 50-assertion Playwright walkthrough at 1440×900 and 390×844. |

Everything is vanilla: no bundler, no framework, no dependencies. Keep it
that way unless there is a strong reason; a single `open index.html` must
keep working.

## Constraints (do not break)

1. Fictional data, simulated AI and submissions, no network calls beyond the
   font host. No model API. No live systems. No deploy.
2. Keep the four required flows working and tested:
   - StarCloud → Paraclete 1 → Pending annuity clients → Okonkwo-Reyes Household, then the case workflow through a simulated submission and issue.
   - Today shows two AUM reviews and one Insurance review; resolving one updates badge, label amber and activity.
   - Conversation answers "what needs attention" and navigates on "open <system>".
   - Phone size works: drawer full width, sidebar as a drawer, no horizontal overflow.
3. `prefers-reduced-motion` turns motion off.
4. `node ../tests/nigel_ui_check.cjs` must stay green. Add assertions for anything new. Set `NIGEL_PROXY` if the machine needs a proxy for Google Fonts; without network the font falls back and that is fine.
5. Do not touch Ace (`../ace2`, `../ace-portal`, `../bot.py`).

## How to see it

```
open index.html
NIGEL_SHOTS=/tmp/nigel-shots node ../tests/nigel_ui_check.cjs   # screenshots of every step
```

The check script is the fastest way to get before/after screenshots at
both sizes. Look at them; do not trust the assertions alone for visual work.

## Upgrade list, in priority order

Brady asked for "a rough draft but functional, like a video game but not at
the same time". The draft is there. These are the upgrades that would move
it most, roughly in order:

1. **Closer to the reference image.** Side-by-side, ours is crisper and more
   wire-like; the reference is dustier, with broader soft nebula and finer,
   more numerous filaments. Knobs are all in `buildBase()`: arc count and
   amplitude, the three stroke passes, dust probability, haze stops. Consider
   a second, finer set of arcs at low alpha, and a subtle horizontal band of
   haze across the middle.
2. **Camera feel.** Add inertia to drag (velocity on release, decay), ease
   wheel zoom, and smooth the fly-to when opening a system. Clamp so the
   focused system never lands under the sidebar or drawer.
3. **Satellite layout.** Positions are fixed screen pixels around the parent.
   Make them zoom-aware and collision-aware (push apart labels that overlap),
   and give the outer ring a gentle slow rotation that pauses on hover.
4. **Minimap / breadcrumb on the map.** A small corner minimap of the nine
   systems with the current viewport rectangle, clickable. Reuse the label
   positions from `data.js`.
5. **Attention on the map.** When an amber system is focused, show its open
   reviews as satellites too (amber), each opening Today scrolled to that
   review.
6. **Drawer polish.** Slide-in from the right with the field easing left;
   sticky breadcrumb; keyboard focus moves into the drawer on open and back
   to the map on close.
7. **Conversation.** Keep it simulated, but let it drive the map: "show me
   Halvorsen" flies to the record satellite and highlights it; "zoom out"
   resets. Add 5–10 more intents; keep replies short.
8. **Performance.** Headless software rendering measured ~35 fps at
   1440×900. Profile `frame()`; candidates: cache the ring/tick drawing to an
   offscreen canvas, cap dust on low-end devices via `deviceMemory`, skip
   comets when `document.hidden`.
9. **Accessibility.** Focus order across labels, satellites and drawer;
   `aria-current` on the active system; live region announcements on route
   change.
10. **Sound (optional, off by default).** A soft UI tick on select and a
    low hum on the StarCloud, with a mute toggle in the HUD. Only if Brady
    wants it; ask first.

## Style notes

- Type is Jost, thin and letterspaced for chrome (10–13px uppercase, 0.18–0.28em), 300 weight for body copy. Do not swap in Inter or a system grotesk.
- Colours come from the tokens at the top of `styles.css`. Amber is only for attention; blue is the accent; ice-white is the highlight.
- Panels are dark glass with 1px blue-tinted lines and 2–4px radii. No big rounded cards, no drop shadows on everything.
- Every simulated thing says so on screen ("sim", "simulated", "NIGEL · simulated"). Keep those labels.

## When you are done

Update `../docs/HANDOFF.md`: what changed, how to open it, what you tested
(with the check script output), and what is still rough. Commit on the
working branch. Do not open a new PR if `pfi-refi/ace-bot#2` is still open;
push to its branch instead.
