# HANDOFF — NIGEL StarCloud prototype

_Last updated: 2026-09-13 (third pass: built against the visual reference, then animated). Branch: `claude/nigel-dashboard-redesign-4tcpd7`._

## Context and assumptions

The request referred to a workspace at `/Users/brady/Documents/Praxis-NIGEL`,
an `AGENTS.md`, a `docs/CURRENT-STATE.md` and an earlier dashboard prototype.
None of those were present in this session; only the `ace-bot` repository was.
The visual reference arrived on the second pass and the presentation was rebuilt
against it. So:

- The prototype lives in `nigel/` inside this repo and was built from scratch.
- The look follows the reference image: near-black ground, blue nebula centred
  on a starburst, galaxy-like filament arcs, a thin full-height light column,
  text-only chrome in a thin letterspaced face, the reference's sidebar list
  (StarCloud, Today, My Office, Clients, Pipeline, Investments, Planning,
  Servicing, Knowledge, GodPod, Settings), the date / time / quote block top
  right, "Praxis Technologies" bottom left, "Discipline today / A brighter
  tomorrow" bottom right, and a narrow centred "Ask NIGEL..." pill with a mic.
- The brief's content was mapped onto the reference's labels: **Paraclete 1**
  is a system on the cloud (ninth label, next to the core); **AUM** is a sector
  of Investments and **Insurance** a sector of Servicing, so those two systems
  carry the amber attention marks.
- This file was created new rather than updated.
- Ace (`ace2/`, `ace-portal/`, `bot.py`) was not touched.
- Nothing was deployed, no live system was connected, no API credits were used.

## What was built

`nigel/` — five static files, no build step, no dependencies, no network.

| Piece | Where | Notes |
|------|-------|-------|
| StarCloud | `starcloud.js` | Canvas. Black ground, elliptical blue haze, ~2,300 stars, 44 noisy elliptical filament arcs drawn as glow plus particle dust, soft dendrites from the column and from each node, faint vertical streaks, a thin full-height column with a starburst and horizontal flare at the core, node blooms, vignette. Seeded so it renders the same every load. Static layers are pre-rendered once per viewport; the camera zooms toward a system when you enter it. Respects `prefers-reduced-motion`. |
| Motion | `starcloud.js` → `initLive`, `frame` | Everything moves: a cinematic fade-and-zoom on load; the whole field sways, breathes and rotates a fraction of a degree; ~340 dust motes drift with depth parallax; ~48 comets travel along the filament arcs with trails; sparks rise inside the column; the core breathes, its flare beams rotate, three holographic tick rings turn at different speeds and a ring pulse expands from the core every few seconds; each system has an orbit with a circling mote (faster and amber when it needs attention); a shooting star crosses every 5–12 s. CSS adds a slow glow pulse on labels, a breathing orb and bar, a pulsing active bullet and attention dot, and a staggered rise-in for the chrome. All of it is off under `prefers-reduced-motion`. |
| System labels | `app.js` → `buildLabels` | Plain uppercase letterspaced text, centred above each node and projected through the camera each frame. An amber dot and an amber "N need attention" line appear when the system has open reviews. Portrait screens use a two-column layout of node positions. |
| Sidebar | `index.html` / `styles.css` | Text only, no panel: NIGEL wordmark and tagline, the reference's eleven items with ring bullets (active item filled, with a soft blue bar), Praxis Technologies and a prototype note at the foot. Off-canvas drawer with a scrim on phones. Today carries the amber badge. |
| Corners | | Top right: live date and time, a rule, an amber "N need attention" link, the quote. Bottom right: the tagline. On panel views the top-right block collapses to one line and the quote and tagline hide. The breadcrumb (StarCloud › Paraclete 1 › Pending annuity clients › household) sits above the panel; on phones it collapses to back-link + current. |
| Views | `app.js` | StarCloud, Today (attention + recent activity), My Office (open cases), Systems, Sectors, Records, Record, Activity, Settings (placeholder). Hash-routed, so every view has a URL and the browser back button works. Escape goes up one level. Sidebar items for Clients, Pipeline, Investments, Planning, Servicing, Knowledge and GodPod open that system's sectors. |
| Sample case | `data.js` | Paraclete 1 → Pending annuity clients → **Okonkwo-Reyes Household** (fixed index annuity, 1035 exchange, $250k, fictional carrier "Northwind Assurance"). Three other fictional households at other stages. |
| Case workflow | `app.js` → `draftMemo`, `illustrate`, `runSubmit` | Intake → Suitability → Illustration → Submission → Issued. "Draft memo" types out a canned memo labelled *simulated AI draft*. "Generate" builds a 10-year illustration table. "Submit" asks for confirmation in a modal that says plainly it is simulated, then animates Packaging → Transmitting → Acknowledged and prints a `SIM-…` receipt. "Reset case" replays it. |
| Attention (Today) | `app.js` → `viewToday` | Two AUM reviews (model drift, fee schedule mismatch) under Investments and one Insurance review (premium lapse) under Servicing. Approve / Request changes / Defer each resolve the item, drop the badge, log to Activity and clear the amber from the system's label. "Reset sample reviews" restores them. |
| Conversation bar | `app.js` → `reply` | Narrow centred pill with an orb and a mic (the mic explains it is not in the prototype). Fixed intent matcher, no model. Understands "what needs attention", "open <system>" (AUM and Insurance alias to Investments and Servicing), "where is the Okonkwo-Reyes case", "pending", "submit", "help". Replies carry chips that navigate and are labelled *NIGEL · simulated*. |
| Type | `index.html` | Jost from Google Fonts (200–500) with a Futura / Avenir / Helvetica fallback stack. Offline the fallback renders; the layout holds either way. |
| Persistence | `sessionStorage` | Review outcomes, case progress and activity survive a reload within the tab. Wrapped in try/catch; runs in memory if storage is unavailable. |

## How to open it

```
open nigel/index.html                    # file:// is fine, no server needed
# or
python3 -m http.server -d nigel 8080     # http://localhost:8080
```

Suggested path for a demo: click **Paraclete 1** on the cloud → **Pending
annuity clients** → **Okonkwo-Reyes Household** → Draft memo → Mark reviewed →
Generate → Client signed → Submit → Simulate submission → simulate issue.
Then **Today** in the sidebar, approve the Insurance item, return to the
StarCloud and watch the Servicing label lose its amber.

## What was tested

`tests/nigel_ui_check.cjs` (Playwright, headless Chromium) runs the full path
above at **1440×900** and **390×844** and screenshots every step. Run with
`node tests/nigel_ui_check.cjs`; screenshots go to `/tmp/nigel-shots` or
`$NIGEL_SHOTS`. Set `NIGEL_PROXY` to an HTTP proxy if the machine needs one to
reach Google Fonts. All 50 assertions pass on the final build:

- 9 labels render; Investments and Servicing are amber; every label sits inside the viewport at both sizes.
- No horizontal overflow on StarCloud, Sectors, Record, or with the conversation log open.
- Label click → Sectors; Sectors → Records (4 rows); Records → Record.
- Memo drafts, illustration renders 10 rows, submission asks for confirmation, receipt shows a `SIM-` reference, case reaches Issued.
- Breadcrumb back works; Escape closes the conversation log, then goes up one level.
- Today shows 3 reviews grouped 2 AUM / 1 Insurance; approving one drops the badge to 2 and clears the Servicing label.
- Conversation answers "what needs attention" and navigates to Investments on "open AUM".
- Phone menu opens and closes; no page errors or console errors at either size.

Screenshots were compared against the reference by eye and iterated: filament
lines were softened and given more dust, the Paraclete 1 label was moved off
the starburst flare, the phone label grid was moved clear of the attention
count, and the top-right block was collapsed on panel views so it no longer
overlaps the panel. Motion was checked by sampling frames at 0.4 s, 2.5 s and
6 s and confirming the canvas changes between frames. Headless software
rendering measured roughly 35 fps at 1440×900 and 60 fps at 390×844; a
GPU-backed browser should hold 60 at both.

## Remaining limitations

- Motion density is a taste call: comet count, dust count, ring alpha and sway amplitude are the first lines of `initLive()` and `frame()` in `starcloud.js`.
- The reference's cloud is a painted image; this one is procedural. It is
  close in structure and palette but softer detail (dust density, the way
  filaments braid) is a matter of taste and easy to tune in `buildBase()`.
- Sidebar items Today and My Office are interpretations: Today = what needs
  you plus recent activity, My Office = open cases. Settings is a placeholder.
- Only Paraclete 1 → Pending annuity clients has records. Other sectors show
  counts and are marked "Not in this prototype".
- The conversation bar is a fixed intent matcher. Anything outside its short
  list gets a fallback that says so.
- State lives in `sessionStorage`: it clears when the tab closes and is not
  shared between tabs or devices.
- Canvas re-renders its static layers on resize; on very large or 3× displays
  the first paint after a resize can take a few hundred milliseconds.
- No automated accessibility audit yet. Labels, nav, breadcrumb and modal are
  keyboard-reachable buttons with ARIA labels, but focus order across the
  canvas labels has not been tuned.
- Not a PWA, no offline manifest, no icons.

## Next steps

1. Confirm the Today / My Office interpretation and the AUM-under-Investments, Insurance-under-Servicing mapping.
2. Decide what the seven other sidebar systems should hold; they currently show sector counts only.
3. Tune the cloud against the reference side by side if wanted (arc count, dust, column glow are the first knobs in `buildBase()`).
