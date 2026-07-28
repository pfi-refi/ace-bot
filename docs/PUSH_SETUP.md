# Phone push notifications — setup

Ace reaches Brady's phone when the app is **closed**. Before this, a proactive brief
only landed if a HUD tab happened to be open (it rode the WebSocket). Now the morning
brief and the evening recap also fire a Web Push, so they show up on the lock screen.

The whole path is **dormant until the two VAPID env vars are set**. With them unset:
`GET /push/key` returns `{"publicKey": null}`, the HUD never offers the prompt, and
`send_push()` logs one warning and returns. Briefs are never blocked, slowed, or failed
by push being unconfigured.

---

## 1. Set three env vars in Railway

Railway → the **ace2** service → **Variables** → *New Variable* (paste, then Deploy):

| Variable            | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| `VAPID_PUBLIC_KEY`  | the 87-char base64url public key from the handoff            |
| `VAPID_PRIVATE_KEY` | the 43-char base64url private key from the handoff — secret  |
| `VAPID_SUBJECT`     | `mailto:pfi@platinumfortuneimpact.com` (optional; this is the default) |

The keys are a matched pair — swapping in a new pair invalidates every existing
subscription, and each device has to tap "Phone alerts" again.

`VAPID_PRIVATE_KEY` is a credential. It lives in Railway only; it is never committed
and never sent to the browser. Only the public key is ever served (`GET /push/key`).

Push subscriptions are stored in Postgres (`push_subs`), so `DATABASE_URL` must be set
too — it already is.

### Regenerating the pair (only if the private key ever leaks)

```bash
python3 - <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
k = ec.generate_private_key(ec.SECP256R1())
print("VAPID_PUBLIC_KEY =", b(k.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)))
print("VAPID_PRIVATE_KEY =", b(k.private_numbers().private_value.to_bytes(32, "big")))
PY
```

## 2. Install the app to the iPhone home screen

**iOS only allows web push for an installed PWA, on iOS 16.4 or newer.** In Safari:
open the portal → Share → **Add to Home Screen** → open Ace *from that icon*. Push will
never work from a Safari tab, no matter what is configured on the server.

Android/Chrome and desktop Chrome/Edge work from a normal tab as well.

## 3. Turn it on

Open Ace (from the home-screen icon) and log in. A **🔔 Phone alerts** button appears in
the dock alongside **Not now**. Tap it → allow the iOS permission prompt → the device
registers itself with the server and both buttons disappear.

The offer is one-time per device, remembered in `localStorage` under `ace2_push`
(`on` / `off`). To be asked again: delete that key in Safari's storage, or reinstall
the app.

## 4. Verify

```bash
TOKEN=$(curl -s -X POST https://<host>/auth -H 'Content-Type: application/json' \
  -d '{"password":"…"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s https://<host>/push/key    -H "Authorization: Bearer $TOKEN"   # publicKey non-null
curl -s -X POST https://<host>/push/test -H "Authorization: Bearer $TOKEN"
```

`/push/test` sends a real notification to every registered device and reports what each
push service said, per endpoint. A phone that has been wiped or uninstalled answers
404/410 and is dropped from the table automatically.

## Endpoints

| Route                   | Purpose                                                      |
| ----------------------- | ------------------------------------------------------------ |
| `GET  /push/key`        | VAPID public key, or `null` when unconfigured                 |
| `POST /push/subscribe`  | store a device's `PushSubscription` (posted verbatim)         |
| `POST /push/unsubscribe`| drop one device by `endpoint`                                 |
| `POST /push/test`       | send a test notification to every device, per-endpoint results|

All four require the normal Bearer token.

## Sending a push from anywhere else in the code

```python
from .main import send_push
send_push("ACE · Heads up", "Miller refi has gone quiet for 9 days.", "/")
```

Fire-and-forget: it hands off to a daemon thread, never blocks, never raises, and does
nothing at all when VAPID is unset. `chat.generate_brief()` already calls it, which
covers both the scheduled brief loop and `POST /brief/run`.

## Troubleshooting

- **No 🔔 button** — server has no VAPID keys, the browser has no `PushManager`
  (iOS < 16.4 or not installed to the home screen), permission was already denied, or
  `ace2_push` is already set in `localStorage`.
- **Subscribe returns `ok:false`** — `DATABASE_URL` missing; there is nowhere to store it.
- **`/push/test` shows 403** — the `VAPID_*` pair doesn't match the key the device
  subscribed with. Re-subscribe the device after any key change.
- **Notification says "This website has been updated in the background"** — iOS's
  placeholder for a push the service worker didn't visibly show. The `push` handler in
  `sw.js` always calls `showNotification`, so this means an old service worker is still
  active: close all instances of the app and reopen.
