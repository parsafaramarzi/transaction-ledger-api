"""Edge cases found by review: integer limits, lock timeouts, trailing slashes, logging."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient
from helpers import BalanceOf, MakeAccount, PostTransaction

from ledger import db
from ledger.api import app
from ledger.logging_config import JsonFormatter, configure_logging

INT64_MAX = 2**63 - 1


def set_balance(account_id: int, balance: int) -> None:
    """Put an account into a state the API alone could never reach quickly."""
    with db.connection() as conn:
        conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (balance, account_id))


# --------------------------------------------------------------------------- #
# F1 / F2: integer limits                                                     #
# --------------------------------------------------------------------------- #
def test_absurdly_large_amount_is_422_not_500(
    make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice")

    assert post_transaction("deposit", 10**30, destination=alice).status_code == 422


def test_amount_just_over_the_maximum_is_rejected_and_the_maximum_is_accepted(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice")

    assert post_transaction("deposit", db.MAX_AMOUNT + 1, destination=alice).status_code == 422
    assert post_transaction("deposit", db.MAX_AMOUNT, destination=alice).status_code == 201
    assert balance_of(alice) == db.MAX_AMOUNT


def test_a_deposit_that_would_overflow_int64_is_rejected_and_the_account_stays_readable(
    client: TestClient,
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    balance_of: BalanceOf,
) -> None:
    alice = make_account("Alice")
    set_balance(alice, INT64_MAX - 5)

    response = post_transaction("deposit", 10, destination=alice)

    assert response.status_code == 422
    assert client.get(f"/accounts/{alice}").status_code == 200  # was a 500 before the fix
    assert balance_of(alice) == INT64_MAX - 5


def test_a_balance_can_reach_exactly_int64_max(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice")
    set_balance(alice, INT64_MAX - 5)

    assert post_transaction("deposit", 5, destination=alice).status_code == 201
    assert balance_of(alice) == INT64_MAX


def test_a_transfer_into_a_full_account_is_rejected_and_the_debit_is_rolled_back(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)
    bob = make_account("Bob")
    set_balance(bob, INT64_MAX - 5)

    response = post_transaction("transfer", 100, source=alice, destination=bob)

    assert response.status_code == 422
    assert balance_of(alice) == 1000  # the debit happened first, then was undone
    assert balance_of(bob) == INT64_MAX - 5


def test_database_refuses_a_non_integer_balance(fresh_db: object) -> None:
    alice = db.create_account("Alice").id

    with db.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE accounts SET balance = 1.5 WHERE id = ?", (alice,))


@pytest.mark.parametrize(
    "path",
    [
        f"/accounts/{10**30}",
        "/accounts/0",
        "/accounts/-1",
        f"/transactions/{10**30}",
        "/transactions/0",
        f"/transactions/?account_id={10**30}",
        f"/transactions/?cursor={10**30}",
    ],
)
def test_out_of_range_ids_are_422_not_500(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


def test_out_of_range_account_ids_in_a_transaction_body_are_422(
    post_transaction: PostTransaction,
) -> None:
    assert post_transaction("deposit", 5, destination=10**30).status_code == 422
    assert post_transaction("withdraw", 5, source=10**30).status_code == 422


# --------------------------------------------------------------------------- #
# F3: a busy database is a retryable 503, not a 500                           #
# --------------------------------------------------------------------------- #
def test_a_locked_database_returns_503_with_retry_after_and_the_request_is_retryable(
    client: TestClient,
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    balance_of: BalanceOf,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = make_account("Alice", 100)
    monkeypatch.setattr(db, "BUSY_TIMEOUT", 0.2)  # don't make the test wait 10 s
    blocker = db.get_connection()
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    try:
        blocked = post_transaction("deposit", 50, destination=alice, key="retry-me")
        blocked_create = client.post("/accounts/", json={"name": "Bob"})
    finally:
        blocker.rollback()
        blocker.close()

    assert blocked.status_code == 503
    assert blocked.headers["retry-after"] == "1"
    assert blocked_create.status_code == 503
    assert balance_of(alice) == 100

    retry = post_transaction("deposit", 50, destination=alice, key="retry-me")
    assert retry.status_code == 201  # the key was not burned by the failed attempt
    assert balance_of(alice) == 150


def test_reads_still_work_while_another_writer_holds_the_lock(
    client: TestClient, make_account: MakeAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = make_account("Alice", 100)
    monkeypatch.setattr(db, "BUSY_TIMEOUT", 0.2)
    blocker = db.get_connection()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        assert client.get(f"/accounts/{alice}").status_code == 200  # WAL: readers don't block
    finally:
        blocker.rollback()
        blocker.close()


# --------------------------------------------------------------------------- #
# F6: collection routes work without a trailing slash, no redirect            #
# --------------------------------------------------------------------------- #
def test_collection_routes_work_without_a_trailing_slash_and_without_redirecting(
    fresh_db: object,
) -> None:
    no_redirects = TestClient(app, follow_redirects=False)

    created = no_redirects.post("/accounts", json={"name": "Alice"})
    assert created.status_code == 201
    account_id = created.json()["account"]["id"]

    deposit = no_redirects.post(
        "/transactions",
        json={"transaction_type": "deposit", "account_destination_id": account_id, "amount": 5},
        headers={"Idempotency-Key": "slashless"},
    )
    assert deposit.status_code == 201

    listing = no_redirects.get(f"/transactions?account_id={account_id}&limit=5")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


# --------------------------------------------------------------------------- #
# F5: logging                                                                 #
# --------------------------------------------------------------------------- #
def events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "ledger.api"]


def test_an_applied_transaction_is_logged_with_its_details(
    make_account: MakeAccount, post_transaction: PostTransaction, caplog: pytest.LogCaptureFixture
) -> None:
    alice = make_account("Alice")
    caplog.set_level(logging.INFO, logger="ledger")
    caplog.clear()

    post_transaction("deposit", 250, destination=alice, key="log-1")

    [record] = events(caplog)
    assert record.getMessage() == "transaction_applied"
    assert record.levelno == logging.INFO
    assert record.__dict__["amount"] == 250
    assert record.__dict__["tx_type"] == "deposit"
    assert record.__dict__["destination_id"] == alice
    assert record.__dict__["idempotency_key"] == "log-1"


def test_a_replay_is_logged_as_a_replay_not_as_a_second_application(
    make_account: MakeAccount, post_transaction: PostTransaction, caplog: pytest.LogCaptureFixture
) -> None:
    alice = make_account("Alice")
    caplog.set_level(logging.INFO, logger="ledger")
    post_transaction("deposit", 10, destination=alice, key="log-2")
    caplog.clear()

    post_transaction("deposit", 10, destination=alice, key="log-2")

    assert [r.getMessage() for r in events(caplog)] == ["transaction_replayed"]


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("insufficient", "insufficient_funds"), ("missing", "account_not_found")],
)
def test_rejections_are_logged_with_a_reason(
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    reason: str,
) -> None:
    alice = make_account("Alice", 10)
    caplog.set_level(logging.INFO, logger="ledger")
    caplog.clear()

    if kind == "insufficient":
        post_transaction("withdraw", 999, source=alice)
    else:
        post_transaction("deposit", 5, destination=999)

    [record] = events(caplog)
    assert record.getMessage() == "transaction_rejected"
    assert record.levelno == logging.WARNING
    assert record.__dict__["reason"] == reason


def test_configure_logging_writes_json_to_stderr_and_is_idempotent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger("ledger")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    logger.handlers.clear()
    monkeypatch.setenv("LEDGER_LOG_LEVEL", "INFO")
    try:
        configure_logging()
        configure_logging()  # a second call must not add a second handler (= duplicate lines)
        logging.getLogger("ledger.api").info("hello", extra={"amount": 7})
        lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    finally:
        logger.handlers[:] = saved[0]
        logger.setLevel(saved[1])
        logger.propagate = saved[2]

    assert len(lines) == 1
    assert json.loads(lines[0])["amount"] == 7


def test_json_formatter_emits_one_parseable_json_object_per_line() -> None:
    record = logging.LogRecord(
        "ledger.api", logging.INFO, __file__, 1, "transaction_applied", (), None
    )
    record.amount = 250
    record.idempotency_key = "k-1"

    line = JsonFormatter().format(record)
    payload = json.loads(line)

    assert "\n" not in line
    assert payload["event"] == "transaction_applied"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "ledger.api"
    assert payload["amount"] == 250
    assert payload["idempotency_key"] == "k-1"
    assert "time" in payload
