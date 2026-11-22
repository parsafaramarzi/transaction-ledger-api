#!/usr/bin/env bash
# Hammer a running ledger with parallel requests and check the promises hold from the outside.
#
#   uvicorn ledger.api:app &              # start the server first
#   ./scripts/load_test.sh                # BASE_URL=http://127.0.0.1:8000 by default
#
# Scenario A: one idempotency key sent 100 times at once    -> applied exactly once
# Scenario B: 200 concurrent withdrawals from a 1000 balance -> exactly 100 succeed, none overdraw
#
# Needs: bash, curl, xargs, sed, awk, sort, uniq. Exits 1 if any promise is broken.
set -euo pipefail

BASE_URL=${BASE_URL:-http://127.0.0.1:8000}
CONCURRENCY=${CONCURRENCY:-20}
failures=0

json_header='Content-Type: application/json'

# new_account NAME -> prints the new account's id
new_account() {
    curl -sf -X POST "$BASE_URL/accounts" -H "$json_header" -d "{\"name\": \"$1\"}" |
        sed -n 's/.*"account": *{"id": *\([0-9]*\).*/\1/p'
}

balance_of() {
    curl -sf "$BASE_URL/accounts/$1" | sed -n 's/.*"balance": *\([0-9]*\).*/\1/p'
}

# fire COUNT KEY_PREFIX BODY [unique]
# Sends COUNT requests, CONCURRENCY at a time, and prints one HTTP status code per request.
# With "unique" each request gets its own key (PREFIX-1, PREFIX-2, ...); otherwise they all
# share PREFIX, which is how a client retry looks to the server.
fire() {
    local count=$1 prefix=$2 body=$3 mode=${4:-same} key
    key=$prefix
    [ "$mode" = unique ] && key="$prefix-{}"
    # xargs -I{} replaces {} in the arguments with each number from seq; -P runs them in parallel.
    # `tr -d '\r'` because a Windows curl ends its lines with \r\n, which would make identical
    # status codes look different to sort | uniq. It is a no-op on Linux and macOS.
    seq 1 "$count" |
        xargs -P "$CONCURRENCY" -I{} \
            curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/transactions" \
            -H "$json_header" -H "Idempotency-Key: $key" -d "$body" |
        tr -d '\r'
}

# count_of CODE -> reads status codes on stdin, prints how many equal CODE
count_of() { grep -c "^$1\$" || true; }

check() { # check DESCRIPTION EXPECTED ACTUAL
    if [ "$2" = "$3" ]; then
        printf '  PASS  %s (%s)\n' "$1" "$3"
    else
        printf '  FAIL  %s (expected %s, got %s)\n' "$1" "$2" "$3"
        failures=$((failures + 1))
    fi
}

command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
curl -sf "$BASE_URL/docs" >/dev/null || { echo "no ledger at $BASE_URL (start it first)" >&2; exit 2; }
run=$(date +%s)-$$ # keeps keys unique across runs against a persistent database

echo "Scenario A: the same idempotency key, 100 requests, $CONCURRENCY at a time"
acct=$(new_account "load-A")
fire 1 "seed-$run" "{\"transaction_type\":\"deposit\",\"account_destination_id\":$acct,\"amount\":1000}" >/dev/null
codes=$(fire 100 "same-$run" "{\"transaction_type\":\"deposit\",\"account_destination_id\":$acct,\"amount\":10}")
echo "$codes" | sort | uniq -c | sed 's/^/    /'
check "exactly one request created the transaction (201)" 1 "$(echo "$codes" | count_of 201)"
check "every other request was a replay (200)" 99 "$(echo "$codes" | count_of 200)"
check "the deposit was applied once: 1000 + 10" 1010 "$(balance_of "$acct")"

echo
echo "Scenario B: 200 concurrent withdrawals of 10 from a balance of 1000"
acct=$(new_account "load-B")
fire 1 "seed-b-$run" "{\"transaction_type\":\"deposit\",\"account_destination_id\":$acct,\"amount\":1000}" >/dev/null
codes=$(fire 200 "w-$run" "{\"transaction_type\":\"withdraw\",\"account_source_id\":$acct,\"amount\":10}" unique)
echo "$codes" | sort | uniq -c | sed 's/^/    /'
check "exactly 100 withdrawals succeeded (201)" 100 "$(echo "$codes" | count_of 201)"
check "the other 100 were refused for insufficient funds (422)" 100 "$(echo "$codes" | count_of 422)"
check "the balance is exactly 0, never negative" 0 "$(balance_of "$acct")"

echo
if [ "$failures" -eq 0 ]; then
    echo "All promises held."
else
    echo "$failures check(s) FAILED."
    exit 1
fi
