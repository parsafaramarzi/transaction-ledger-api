#!/usr/bin/env bash
# Summarise the ledger's JSON log lines using only classic Unix tools.
#
#   ./scripts/analyze_logs.sh service.log
#   docker logs ledger 2>&1 | ./scripts/analyze_logs.sh
#   journalctl -u ledger -o cat | ./scripts/analyze_logs.sh
#
# Each log line is one JSON object, e.g.
#   {"time": "...", "level": "INFO", "logger": "ledger.api", "event": "transaction_applied",
#    "idempotency_key": "k1", "tx_type": "deposit", "amount": 500, "source_id": null, ...}
# The pipelines below are the point: grep selects lines, sed/cut extract a field,
# sort | uniq -c counts, and awk sums.
#
# Two shell pitfalls this script is written around (both bit it during testing):
#   * `set -e -o pipefail` makes a pipeline fail when ANY stage fails, and grep "fails"
#     (exit 1) whenever it finds nothing. So every grep in a pipeline goes through `matches`.
#   * `head` closes the pipe as soon as it has enough lines; the stages before it then die
#     of SIGPIPE (exit 141) and pipefail reports that as a failure. `first` reads everything.
set -euo pipefail

# grep that treats "no match" as an empty result, not an error.
matches() { grep "$@" || true; }

# Print only the first N lines, but keep reading so earlier stages never get SIGPIPE.
first() { awk -v n="$1" 'NR <= n'; }

# Print stdin, or "(none)" when there is nothing to show.
or_none() {
    local text
    text=$(cat)
    printf '%s\n' "${text:-  (none)}"
}

# Keep only the JSON lines (uvicorn's own startup lines are plain text).
lines=$(matches '^{' "${1:-/dev/stdin}")

if [ -z "$lines" ]; then
    echo "no JSON log lines found" >&2
    exit 1
fi

# The lines that contain the literal event name given as $1.
with_event() { printf '%s\n' "$lines" | matches "\"event\": \"$1\""; }

echo "== events =="
printf '%s\n' "$lines" |
    matches -o '"event": "[a-z_]*"' |
    cut -d'"' -f4 |
    sort | uniq -c | sort -rn

echo
echo "== why transactions were rejected =="
with_event transaction_rejected |
    matches -o '"reason": "[a-z_]*"' |
    cut -d'"' -f4 |
    sort | uniq -c | sort -rn |
    or_none

echo
echo "== money applied, by type (minor units) =="
# Pull "type amount" pairs out of each applied line, then sum the amounts per type with awk.
with_event transaction_applied |
    sed -n 's/.*"tx_type": "\([a-z]*\)".*"amount": \([0-9]*\).*/\1 \2/p' |
    awk '{ total[$1] += $2; count[$1]++ }
         END { for (t in total) printf "%-10s %12d  (%d transactions)\n", t, total[t], count[t] }' |
    sort |
    or_none

echo
echo "== most-used idempotency keys (a count above 1 means replays/retries) =="
printf '%s\n' "$lines" |
    matches -E '"event": "transaction_(applied|replayed)"' |
    matches -o '"idempotency_key": "[^"]*"' |
    cut -d'"' -f4 |
    sort | uniq -c | sort -rn |
    first 5 |
    or_none
