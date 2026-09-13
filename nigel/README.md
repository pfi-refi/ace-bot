# NIGEL — StarCloud prototype

A static, desktop-first prototype of the NIGEL experience: an edge-to-edge
blue / ice-white StarCloud with branching light, a central light column,
floating system labels, a restrained left sidebar and a bottom conversation bar.

**Everything here is fictional and simulated.** No live systems, no model,
no network calls. Nothing is sent anywhere.

## Open it

No build step. Either:

```
open nigel/index.html            # macOS — file:// works
python3 -m http.server -d nigel 8080   # then http://localhost:8080
```

## Navigate

StarCloud → Systems → Sectors → Records. Click a label on the cloud, use the
sidebar, the breadcrumb, the conversation bar ("what needs attention",
"open AUM", "where is the Okonkwo-Reyes case"), or the browser back button.
Every view has a URL hash, e.g. `#/systems/paraclete/pending/okonkwo-reyes`.
Escape goes up one level.

Amber means something needs attention. The sample attention set is two AUM
reviews and one Insurance review.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Shell: sidebar, top bar, stage, conversation bar, modal |
| `styles.css` | Presentation, desktop first with phone breakpoints |
| `starcloud.js` | Canvas renderer for the StarCloud (seeded, no assets) |
| `app.js` | Hash routing, views, simulated workflow and conversation |
| `data.js` | Fictional systems, sectors, households, reviews |

Browser check: `node tests/nigel_ui_check.cjs` (Playwright, desktop + phone).
