#!/bin/zsh
set -e
HERE="${0:A:h}"; ROOT="${HERE:h}"
# Prefer a venv that actually exists: the shared runtime one, else this tree's own.
V="/Users/brady/Documents/Codex/2026-09-06/install-github-cli-gh-on-this/work/ace-repair/.venv-runtime/bin/python"
[ -x "$V" ] || V="$ROOT/.venv-test/bin/python"
NODE="/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
LOG="${TMPDIR:-/tmp}/ace-voice-ui.log"
mkdir -p "${ACE_UI_OUT:-/tmp/ace-voice-shots}"
pkill -f "tests/ui_server.py" 2>/dev/null || true
sleep 1
( cd "$ROOT" && env -u DATABASE_URL -u ANTHROPIC_API_KEY "$V" tests/ui_server.py > "$LOG" 2>&1 & )
for i in {1..40}; do grep -q READY "$LOG" 2>/dev/null && break; sleep 0.5; done
grep -q READY "$LOG" || { echo "server did not start"; tail -20 "$LOG"; exit 1; }
cd "$ROOT" && "$NODE" tests/voice_actions_ui.cjs; rc=$?
if [ $rc -eq 0 ]; then "$NODE" tests/phone_fit_ui.cjs; rc=$?; fi
if [ $rc -eq 0 ]; then "$NODE" tests/voice_continuity_ui.cjs; rc=$?; fi
pkill -f "tests/ui_server.py" 2>/dev/null || true
exit $rc
