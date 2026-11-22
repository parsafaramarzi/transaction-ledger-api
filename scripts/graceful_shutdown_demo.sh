#!/usr/bin/env bash
# Show the difference between a graceful stop (SIGTERM) and a hard kill (SIGKILL) while a
# request is in flight. Needs a POSIX system (Linux, macOS, WSL); Windows has no SIGTERM.
#
#   ./scripts/graceful_shutdown_demo.sh
#
# How it makes a request slow on purpose: another process holds SQLite's write lock for 3 s,
# so a deposit sent in the meantime has to wait. We stop the server while it waits.
#
#   SIGTERM -> the in-flight deposit still completes (201), new connections are refused, and
#              the server then exits with status 143. That is 128 + 15 (SIGTERM): uvicorn
#              finishes its work and re-raises the signal so a supervisor can see it stopped
#              because it was asked to. (It is not a crash; systemd treats it as a clean stop.)
#   SIGKILL -> the process dies at once: the client gets no answer, and because the deposit
#              was never committed, nothing was applied (atomicity).
set -euo pipefail

cd "$(dirname "$0")/.."
PORT=${PORT:-8765}
BASE="http://127.0.0.1:$PORT"
failures=0
work=$(mktemp -d)
server_pid=
trap 'kill -KILL "$server_pid" 2>/dev/null || true; rm -rf "$work"' EXIT

check() { # check DESCRIPTION EXPECTED ACTUAL
    if [ "$2" = "$3" ]; then
        printf '  PASS  %s (%s)\n' "$1" "$3"
    else
        printf '  FAIL  %s (expected %s, got %s)\n' "$1" "$2" "$3"
        failures=$((failures + 1))
    fi
}

scenario() { # scenario TERM|KILL
    local signal=$1
    local db="$work/$signal.db"
    export LEDGER_DB_PATH=$db
    echo "== stopping the server with SIG$signal while a request is in flight =="

    # `bash script` rather than `./script`, so this works even if the executable bit was lost
    # (git on Windows often records scripts as non-executable).
    PORT=$PORT PIDFILE="$work/$signal.pid" bash scripts/run_server.sh >"$work/$signal.log" 2>&1 &
    local wrapper=$!
    for _ in $(seq 1 50); do
        curl -sf "$BASE/docs" >/dev/null 2>&1 && break
        sleep 0.2
    done
    curl -sf "$BASE/docs" >/dev/null || { echo "server did not start" >&2; cat "$work/$signal.log" >&2; exit 2; }
    server_pid=$(cat "$work/$signal.pid")

    curl -s -o /dev/null -X POST "$BASE/accounts" -H 'Content-Type: application/json' -d '{"name":"demo"}'

    # Hold the write lock for 3 seconds from a separate process...
    python - "$db" <<'PY' &
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
time.sleep(3)
conn.execute("ROLLBACK")
PY
    local locker=$!
    sleep 0.7

    # ...send a deposit that has to wait for it...
    curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/transactions" \
        -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
        -d '{"transaction_type":"deposit","account_destination_id":1,"amount":500}' \
        >"$work/$signal.status" 2>/dev/null &
    local request=$!
    sleep 0.7

    # ...and stop the server while the deposit is still waiting.
    if [ "$signal" = TERM ]; then
        kill -TERM "$wrapper"
    else
        kill -KILL "$server_pid"
    fi
    sleep 0.5
    local refused=0
    curl -s -o /dev/null --max-time 2 "$BASE/docs" || refused=$?

    wait "$locker" || true
    wait "$request" || true
    local server_status=0
    wait "$wrapper" || server_status=$?

    local http_status
    http_status=$(cat "$work/$signal.status")
    local balance
    balance=$(python - "$db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT balance FROM accounts WHERE id = 1").fetchone()[0])
PY
)
    echo "  in-flight request answered with HTTP '$http_status'; server exit status $server_status; balance $balance"
    if [ "$signal" = TERM ]; then
        check "the in-flight deposit completed (201)" 201 "$http_status"
        check "the server stopped on SIGTERM after finishing its work (143 = 128 + 15)" 143 "$server_status"
        check "the deposit was applied" 500 "$balance"
        check "new connections were refused while stopping" 1 "$([ "$refused" -ne 0 ] && echo 1 || echo 0)"
    else
        check "the client got no answer (000)" 000 "$http_status"
        check "the server died from SIGKILL (137)" 137 "$server_status"
        check "nothing was applied: the deposit never committed" 0 "$balance"
    fi
    echo
}

scenario TERM
scenario KILL

if [ "$failures" -eq 0 ]; then
    echo "Graceful stop finishes in-flight work; a hard kill loses it, safely."
else
    echo "$failures check(s) FAILED."
    exit 1
fi
