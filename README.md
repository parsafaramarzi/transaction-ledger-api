# Transaction Ledger API

A small HTTP service that keeps account balances and records every movement of money
between accounts. It is built so that **money is never created, lost, or applied twice**,
even when requests are retried or arrive at the same moment.

- **FastAPI** + **SQLite** (raw SQL, no ORM), Python 3.11+
- Money is stored as **integer minor units** (e.g. cents), never floats
- **Idempotent** transaction creation via the `Idempotency-Key` header
- **Atomic**: every transaction is applied in a single database transaction
- Cursor-based pagination
- Typed end to end (`mypy --strict`), linted with `ruff`, tested with `pytest`

## Run it

### Locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

uvicorn ledger.api:app --reload
```

The API is now on <http://127.0.0.1:8000>, with interactive docs at
<http://127.0.0.1:8000/docs>. The database is created on startup in
`./transaction_ledger.db`; set `LEDGER_DB_PATH` to put it somewhere else.

### With Docker

```bash
docker build -t ledger .
docker run --rm -p 8000:8000 -v ledger-data:/data ledger
```

The SQLite file lives on the `ledger-data` volume, so balances survive restarts.

## Try it

```bash
# create two accounts (they start at balance 0)
curl -X POST localhost:8000/accounts/ -H "Content-Type: application/json" -d '{"name": "Alice"}'
curl -X POST localhost:8000/accounts/ -H "Content-Type: application/json" -d '{"name": "Bob"}'

# deposit 10.00 (= 1000 cents) into account 1
curl -X POST localhost:8000/transactions/ \
  -H "Content-Type: application/json" -H "Idempotency-Key: deposit-001" \
  -d '{"transaction_type": "deposit", "account_destination_id": 1, "amount": 1000}'

# move 2.50 from account 1 to account 2
curl -X POST localhost:8000/transactions/ \
  -H "Content-Type: application/json" -H "Idempotency-Key: transfer-001" \
  -d '{"transaction_type": "transfer", "account_source_id": 1, "account_destination_id": 2, "amount": 250}'

curl localhost:8000/accounts/1                  # {"id":1,"name":"Alice","balance":750}
curl "localhost:8000/transactions/?account_id=1&limit=10"
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/accounts/` | Create an account. Body: `{"name": "..."}`. The server assigns the id; the balance starts at `0`. → `201` |
| `GET` | `/accounts/{id}` | An account and its current balance. → `200` / `404` |
| `POST` | `/transactions/` | Create a transaction. Requires the `Idempotency-Key` header. → `201` / `200` (replay) |
| `GET` | `/transactions/{id}` | One transaction. → `200` / `404` |
| `GET` | `/transactions/` | Newest-first page of transactions. Query: `account_id`, `limit` (1–100, default 10), `cursor` |

### Transactions

| `transaction_type` | `account_source_id` | `account_destination_id` | Effect |
|---|---|---|---|
| `deposit` | must be omitted | required | money enters the destination from outside |
| `withdraw` | required | must be omitted | money leaves the source to outside |
| `transfer` | required | required (different account) | money moves between two accounts |

`amount` is a positive integer in minor units.

### Status codes

| Code | When |
|---|---|
| `201` | Transaction created (or account created) |
| `200` | Idempotent replay: same key, same body. The original transaction is returned and nothing is applied again |
| `404` | Unknown account |
| `409` | Idempotency key reused with a **different** body |
| `422` | Invalid request: bad type or amount, missing/empty `Idempotency-Key`, wrong source/destination for the type, or **insufficient funds** |

### Idempotency

Clients generate a unique key (a UUID works) once per logical operation and resend the same
key when they retry. If a response is lost, retrying is always safe:

- same key + same body → `200`, the original transaction, balances unchanged
- same key + different body → `409`
- a *failed* request (e.g. insufficient funds) does not consume its key

### Pagination

Results are newest first. The response is `{"items": [...], "next_cursor": 41}`. Pass
`next_cursor` back as `?cursor=41` to get the next page; `next_cursor` is `null` on the last
page. A non-integer or non-positive `cursor`, or a `limit` outside 1–100, is a `422`.

## How correctness is guaranteed

All money movement goes through `ledger.db.apply_transaction`, which runs in **one SQLite
transaction opened with `BEGIN IMMEDIATE`**:

1. look up the idempotency key (replay / conflict check)
2. check that both accounts exist
3. debit with `UPDATE accounts SET balance = balance - :amount WHERE id = :src AND balance >= :amount`
   — the balance check and the debit are a single statement
4. credit the destination
5. insert the transaction row
6. `COMMIT`

- `BEGIN IMMEDIATE` takes the write lock before step 1, so concurrent requests are
  serialised: two requests can't both read the same balance and both spend it, and two requests
  with the same key can't both pass the replay check.
- Any failure rolls the whole thing back, so there is never a half-applied transfer and never
  money moved without its transaction row (which would make a client's retry apply it twice).
- The schema backs this up even if application code has a bug: `CHECK (balance >= 0)`,
  `CHECK (amount > 0)`, `UNIQUE (idempotency_key)`, and foreign keys on both accounts.

## Development

```bash
pytest                       # tests
ruff check . && ruff format --check .
mypy                         # strict mode, configured in pyproject.toml
```

The suite includes concurrency tests (parallel withdrawals can never overdraw; parallel
requests with one key apply exactly once; opposing transfers don't deadlock or lose money)
and a crash-injection test (a failure after the balances change but before the row is written
leaves everything untouched).

CI (`.github/workflows/ci.yml`) runs lint, type checking and tests on Python 3.11 and 3.13,
and builds the Docker image, on every push and pull request.

## Project layout

```
src/ledger/
    db.py      schema, business rules, apply_transaction (all SQL lives here)
    api.py     FastAPI app: validation and error-to-status mapping only
tests/
    test_api.py   behaviour through HTTP
    test_db.py    atomicity, concurrency and constraint tests below the HTTP layer
```
