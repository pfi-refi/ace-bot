#!/bin/zsh
# Fresh disposable Postgres + the real app + the browser walkthrough. Nothing here touches
# production: the store is created and destroyed by tests/ui_server.py.
set -e
HERE="${0:A:h}"; ROOT="${HERE:h}"
V="/Users/brady/Documents/Codex/2026-09-06/install-github-cli-gh-on-this/work/ace-repair/.venv-runtime/bin/python"
[ -x "$V" ] || V="$ROOT/.venv-test/bin/python"
NODE="/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
LOG="${TMPDIR:-/tmp}/ace-ui-server.log"
pkill -f "tests/ui_server.py" 2>/dev/null || true
sleep 1
( cd "$ROOT" && env -u DATABASE_URL -u ANTHROPIC_API_KEY "$V" tests/ui_server.py > "$LOG" 2>&1 & )
for i in {1..40}; do grep -q READY "$LOG" 2>/dev/null && break; sleep 0.5; done
grep -q READY "$LOG" || { echo "server did not start"; tail -20 "$LOG"; exit 1; }
cd "$ROOT" && "$NODE" tests/release_one_ui.cjs; rc=$?
pkill -f "tests/ui_server.py" 2>/dev/null || true
exit $rc
