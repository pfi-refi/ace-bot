# HANDOFF — NIGEL StarCloud prototype

_Last updated: 2026-09-13. Branch: `claude/nigel-dashboard-redesign-4tcpd7`._

## Context and assumptions

The request referred to a workspace at `/Users/brady/Documents/Praxis-NIGEL`,
an `AGENTS.md`, a `docs/CURRENT-STATE.md`, an earlier dashboard prototype and
an attached visual reference. None of those were present in this session; only
the `ace-bot` repository was. So:

- The prototype was rebuilt from scratch in `nigel/` inside this repo, from the
  written brief (blue / ice-white StarCloud, dense branching light, central
  vertical column, floating system labels, restrained left sidebar, bottom
  conversation bar, amber = attention).
- This file was created new rather than updated.
- Ace (`ace2/`, `ace-portal/`, `bot.py`) was not touched.
- Nothing was deployed, no live system was connected, no API credits were used.

## What was built

`nigel/` — five static files, no build step, no dependencies, no network.

| Piece | Where | Notes |
|------|-------|-------|
| StarCloud | `starcloud.js` | Canvas. Sky gradient, blue/ice haze, ~1,400+ stars, dendritic light structures grown from the column and from each system node, central vertical light column with drifting shimmer, node blooms. Seeded so it renders the same every load. Static layers are pre-rendered once per viewport; the camera zooms toward a system when you enter it. Respects `prefers-reduced-motion`. |
| System labels | `app.js` → `buildLabels` | DOM buttons projected through the camera each frame, so they stay pinned to their node while the view zooms. Amber core and "N need attention" subtitle when the system has open reviews. Portrait screens stack the text under the node; desktop flips labels on the right half so text never leaves the viewport. |
| Sidebar | `index.html` / `styles.css` | Brand, five items (StarCloud, Attention with amber badge, Systems, Records, Activity), "Simulation" pill. Off-canvas drawer with a scrim on phones. |
| Top bar | | Breadcrumb (StarCloud › Systems › Paraclete 1 › Pending annuity clients › household) plus an attention chip. On phones the breadcrumb collapses to back-link + current. |
| Views | `app.js` | StarCloud, Systems, Sectors, Records (list), Record (detail), Attention, Activity. Hash-routed, so every view has a URL and the browser back button works. Escape goes up one level. |
| Sample case | `data.js` | Paraclete 1 → Pending annuity clients → **Okonkwo-Reyes Household** (fixed index annuity, 1035 exchange, $250k, fictional carrier "Northwind Assurance"). Three other fictional households at other stages. |
| Case workflow | `app.js` → `draftMemo`, `illustrate`, `runSubmit` | Intake → Suitability → Illustration → Submission → Issued. "Draft memo" types out a canned memo labelled *simulated AI draft*. "Generate" builds a 10-year illustration table. "Submit" asks for confirmation in a modal that says plainly it is simulated, then animates Packaging → Transmitting → Acknowledged and prints a `SIM-…` receipt. "Reset case" replays it. |
| Attention | `app.js` → `viewAttention` | Two AUM reviews (model drift, fee schedule mismatch) and one Insurance review (premium lapse). Approve / Request changes / Defer each resolve the item, drop the badge, log to Activity and turn the system's label from amber back to ice-white. "Reset sample reviews" restores them. |
| Conversation bar | `app.js` → `reply` | Fixed intent matcher, no model. Understands "what needs attention", "open <system>", "where is the Okonkwo-Reyes case", "pending", "submit", "help". Replies carry chips that navigate. Labelled *NIGEL · simulated*. |
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
Then **Attention** in the sidebar, approve the Insurance item, return to the
StarCloud and watch the Insurance label lose its amber.

## What was tested

`tests/nigel_ui_check.cjs` (Playwright, headless Chromium) runs the full path
above at **1440×900** and **390×844** and screenshots every step. Run with
`node tests/nigel_ui_check.cjs`; screenshots go to `/tmp/nigel-shots` or
`$NIGEL_SHOTS`. All 50 assertions pass on the final build:

- 8 labels render; AUM and Insurance are amber; every label sits inside the viewport at both sizes.
- No horizontal overflow on StarCloud, Sectors, Record, or with the conversation log open.
- Label click → Sectors; Sectors → Records (4 rows); Records → Record.
- Memo drafts, illustration renders 10 rows, submission asks for confirmation, receipt shows a `SIM-` reference, case reaches Issued.
- Breadcrumb back works; Escape closes the conversation log, then goes up one level.
- Attention shows 3 reviews grouped 2 AUM / 1 Insurance; approving one drops the badge to 2 and clears the Insurance label.
- Conversation answers "what needs attention" and navigates on "open AUM".
- Phone menu opens and closes; no page errors or console errors at either size.

Screenshots were inspected by eye and the layout was iterated three times:
flipped labels were re-anchored to their node, portrait labels were changed
from flip to stack to stop the two columns colliding, the Ledger node was moved
off the bottom chips, the workflow action grid was fixed, and the veil behind
panels was lightened so the cloud still reads through.

## Remaining limitations

- **The visual reference was not available**, so the look is built from the
  written description. Expect a round of tuning once the reference is in hand:
  column width and brightness, haze colour, star density, label typography.
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

## Next steps when the reference arrives

1. Put the reference image beside `desktop-1-starcloud.png` from the test run and adjust `buildBase()` in `starcloud.js` (haze stops, column gradient, branch pass widths).
2. Decide whether the column should sit at window centre (current) or at the centre of the content area right of the sidebar.
3. Fill in the other sectors with sample records if the demo needs them.
