#!/usr/bin/env bash
# Run the ledger and shut it down cleanly when asked.
#
#   ./scripts/run_server.sh                 # HOST=0.0.0.0 PORT=8000 by default
#   PIDFILE=/tmp/ledger.pid ./scripts/run_server.sh
#
# Why a wrapper: when something sends this script SIGTERM (docker stop, systemd, `kill`), bash
# does NOT pass it on to the server it started. `trap` catches the signal and forwards it, so
# uvicorn can finish in-flight requests and exit on its own, and this script exits with
# uvicorn's real exit status (143 after a SIGTERM: uvicorn finishes, then re-raises the signal).
set -euo pipefail

python -m uvicorn ledger.api:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" &
child=$!
[ -n "${PIDFILE:-}" ] && echo "$child" >"$PIDFILE"

# shellcheck disable=SC2329  # called indirectly, by the trap below
forward() {
    echo "run_server: got a signal, asking the server (pid $child) to stop gracefully" >&2
    kill -TERM "$child" 2>/dev/null || true
}
trap forward TERM INT

# `wait` returns as soon as a trapped signal arrives, not when the child exits. So wait, then
# wait again: the second call blocks until the server has really finished and gives its status.
status=0
wait "$child" || status=$?
if kill -0 "$child" 2>/dev/null; then
    wait "$child" || status=$?
fi
exit "$status"
