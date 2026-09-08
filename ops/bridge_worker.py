#!/usr/bin/env python3
"""ACE MAX BRIDGE — worker half.

Runs on Brady's iMac via launchd (every 15 min). Pulls due background jobs from
Ace's Railway server (briefs + learning sweeps, with the server's own prompts),
runs the model calls through Claude Code on Brady's Max plan (subscription usage; verify eligibility and limits), and
posts results back — the server files them through its normal guarded stores.

Fails SILENT and SAFE at every step: no claude CLI, no key in Railway, server
unreachable, Mac asleep — the server's own loops cover everything on the API,
exactly as before the bridge existed. Authentication is preflighted before claiming; server claim recovery still needs repair.

Stop it forever:  launchctl unload ~/Library/LaunchAgents/com.pfi.ace-bridge.plist
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, "ace-bridge")
LOG = os.path.join(DIR, "bridge.log")
CONFIG = os.path.join(DIR, "config.json")
PENDING = os.path.join(DIR, "pending")   # results whose POST failed after the model ran


def log(msg: str) -> None:
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 262144:  # trim at 256KB
            os.rename(LOG, LOG + ".1")
        with open(LOG, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def find_claude(cfg) -> str:
    cand = (cfg.get("claude_bin") or "").strip()
    if cand and os.path.exists(os.path.expanduser(cand)):
        return os.path.expanduser(cand)
    hit = shutil.which("claude")
    if hit:
        return hit
    for p in ("~/.local/bin/claude", "/usr/local/bin/claude", "/opt/homebrew/bin/claude",
              "~/.claude/local/claude"):
        px = os.path.expanduser(p)
        if os.path.exists(px):
            return px
    return ""


def api(cfg, path, body=None):
    req = urllib.request.Request(
        cfg["base"] + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Bridge-Key": cfg["key"]},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def think(claude_bin, model, prompt) -> str:
    """One model call through Claude Code headless on the Max plan."""
    r = subprocess.run(
        [claude_bin, "-p", prompt, "--model", model, "--output-format", "text"],
        capture_output=True, text=True, timeout=420)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {(r.stderr or '')[:200]}")
    return (r.stdout or "").strip()


def post_result(cfg, payload, tries=3):
    """Deliver a finished job result, retrying the POST only — never the model call.

    Ten of the 62 bridge failures on record were `The read operation timed out` on this
    POST: the Max-plan model work had ALREADY been done and the answer was thrown away,
    while the server still counted the job as claimed. Re-running `think` would spend
    the work twice, so the retry is on delivery alone, and a result that still cannot be
    delivered is parked on disk for the next run instead of being dropped.
    """
    last = None
    for attempt in range(tries):
        try:
            res = api(cfg, "/bridge/complete", payload)
            # An HTTP 200 is not an acknowledgement. The server answers {"ok": false} when
            # it declined the result — stale period, already delivered, retry later — and
            # treating that as delivered silently discarded finished Max-plan work.
            if isinstance(res, dict) and res.get("ok") is False and not res.get("stale") \
                    and not res.get("late") and res.get("retry") is not False:
                last = RuntimeError(f"server declined: {res.get('reason') or res}")
                if attempt < tries - 1:
                    time.sleep(2 * (3 ** attempt))
                    continue
                break
            return res
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(2 * (3 ** attempt))          # 2s, 6s — bounded, not a hot loop
    try:
        os.makedirs(PENDING, exist_ok=True)
        name = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{payload.get('job', 'job')}.json"
        with open(os.path.join(PENDING, name), "w") as f:
            json.dump(payload, f)
        log(f"result undeliverable ({last}) — parked at pending/{name} for the next run")
    except Exception as e:
        log(f"result LOST — could not park it: {e} (original: {last})")
    return {"parked": True}


def flush_pending(cfg) -> None:
    """Deliver anything parked by an earlier run, before claiming new work."""
    try:
        names = sorted(os.listdir(PENDING)) if os.path.isdir(PENDING) else []
    except Exception:
        return
    for name in names:
        path = os.path.join(PENDING, name)
        try:
            with open(path) as f:
                payload = json.load(f)
        except Exception:
            continue
        try:
            res = api(cfg, "/bridge/complete", payload)
        except Exception as e:
            log(f"parked result {name} still undeliverable: {e}")
            return                                     # server is down; try again next tick
        accepted = not isinstance(res, dict) or res.get("ok") is not False
        # Discard only what can never succeed: a result for a period that has passed, or one
        # the server already delivered. Anything else stays parked — a negative answer we
        # might yet recover from must not destroy finished work.
        terminal = isinstance(res, dict) and (res.get("stale") or res.get("late")
                                              or res.get("retry") is False)
        if accepted or terminal:
            os.remove(path)
            log(f"parked result {name} {'delivered' if accepted else 'dropped'} -> {res}")
        else:
            log(f"parked result {name} declined, keeping it: {res}")
            return


def main() -> int:
    try:
        with open(CONFIG) as config_file:
            cfg = json.load(config_file)
    except Exception as e:
        log(f"no config: {e}")
        return 0
    claude_bin = find_claude(cfg)
    if not claude_bin:
        log("claude CLI not installed yet — idle (install: https://claude.ai/install.sh)")
        return 0
    # Never claim server jobs when the exact configured CLI cannot authenticate.
    try:
        status = subprocess.run([claude_bin, "auth", "status"], capture_output=True,
                                text=True, timeout=15)
        auth = json.loads(status.stdout or "{}")
        if status.returncode != 0 or auth.get("loggedIn") is not True:
            log("authentication unavailable — no jobs claimed; log in with the configured CLI")
            return 1
    except Exception:
        log("authentication preflight failed — no jobs claimed")
        return 1
    flush_pending(cfg)
    try:
        data = api(cfg, "/bridge/jobs")
    except urllib.error.HTTPError as e:
        if e.code == 503:
            log("bridge dormant — ACE2_BRIDGE_KEY not set in Railway yet")
        elif e.code == 401:
            log("bridge key MISMATCH — Railway ACE2_BRIDGE_KEY differs from config.json")
        else:
            log(f"jobs fetch HTTP {e.code}")
        return 0
    except Exception as e:
        log(f"server unreachable: {e}")
        return 0
    jobs = data.get("jobs") or []
    if not jobs:
        log("no due jobs")
        return 0
    model = cfg.get("model") or "sonnet"
    for job in jobs:
        try:
            if job.get("job") == "brief":
                text = think(claude_bin, model, job["prompt"])
                res = post_result(cfg, {"job": "brief", "kind": job["kind"], "text": text,
                                        "job_id": job.get("job_id", ""),
                                        "job_date": job.get("job_date", ""),
                                        "lease_token": job.get("lease_token", "")})
                log(f"brief:{job['kind']} -> {res}")
            elif job.get("job") == "sweep":
                facts = think(claude_bin, model, job["facts_prompt"])
                triage = think(claude_bin, model, job["triage_prompt"])
                reflection = think(claude_bin, model, job["reflection_prompt"])
                res = post_result(cfg, {"job": "sweep", "hash": job.get("hash") or 0,
                                        "facts": facts, "triage": triage,
                                        "reflection": reflection,
                                        "job_id": job.get("job_id", ""),
                                        "job_date": job.get("job_date", ""),
                                        "lease_token": job.get("lease_token", "")})
                log(f"sweep -> {res}")
        except Exception as e:
            log(f"job {job.get('job')} failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
