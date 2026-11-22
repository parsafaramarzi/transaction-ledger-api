"""Tests for the ledger rules, below the HTTP layer."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ledger import db


def fund(account_id: int, amount: int, key: str) -> None:
    db.apply_transaction(
        idempotency_key=key,
        transaction_type="deposit",
        amount=amount,
        source_id=None,
        destination_id=account_id,
    )


def balance(account_id: int) -> int:
    account = db.get_account(account_id)
    assert account is not None
    return account.balance


@pytest.mark.usefixtures("fresh_db")
def test_a_failure_while_recording_the_transaction_rolls_back_the_balances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates a crash after the balances changed but before the row is written."""
    alice = db.create_account("Alice").id
    bob = db.create_account("Bob").id
    fund(alice, 1000, "seed")

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(db, "_insert_transaction", explode)

    with pytest.raises(RuntimeError):
        db.apply_transaction(
            idempotency_key="k",
            transaction_type="transfer",
            amount=400,
            source_id=alice,
            destination_id=bob,
        )

    assert balance(alice) == 1000  # debit undone
    assert balance(bob) == 0  # credit undone
    page, _ = db.list_transactions()
    assert [t.idempotency_key for t in page] == ["seed"]  # no record, and none needed


@pytest.mark.usefixtures("fresh_db")
def test_a_crash_leaves_the_key_unused_so_a_retry_applies_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = db.create_account("Alice").id
    real_insert: Callable[..., db.Transaction] = db._insert_transaction

    def explode_once(*args: Any, **kwargs: Any) -> db.Transaction:
        monkeypatch.setattr(db, "_insert_transaction", real_insert)  # only the first call fails
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(db, "_insert_transaction", explode_once)
    request: dict[str, Any] = {
        "idempotency_key": "k",
        "transaction_type": "deposit",
        "amount": 250,
        "source_id": None,
        "destination_id": alice,
    }

    with pytest.raises(RuntimeError):
        db.apply_transaction(**request)
    assert balance(alice) == 0

    _, created = db.apply_transaction(**request)  # the client's retry
    assert created
    assert balance(alice) == 250


@pytest.mark.usefixtures("fresh_db")
def test_concurrent_withdrawals_never_overdraw() -> None:
    alice = db.create_account("Alice").id
    fund(alice, 100, "seed")

    def withdraw(i: int) -> str:
        try:
            db.apply_transaction(
                idempotency_key=f"w-{i}",
                transaction_type="withdraw",
                amount=10,
                source_id=alice,
                destination_id=None,
            )
        except db.InsufficientFundsError:
            return "rejected"
        return "applied"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(withdraw, range(40)))

    assert outcomes.count("applied") == 10
    assert outcomes.count("rejected") == 30
    assert balance(alice) == 0


@pytest.mark.usefixtures("fresh_db")
def test_concurrent_requests_with_the_same_key_create_one_transaction() -> None:
    alice = db.create_account("Alice").id

    def deposit(_: int) -> tuple[int, bool]:
        transaction, created = db.apply_transaction(
            idempotency_key="one-key",
            transaction_type="deposit",
            amount=50,
            source_id=None,
            destination_id=alice,
        )
        return transaction.id, created

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(deposit, range(16)))

    assert len({transaction_id for transaction_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert balance(alice) == 50


@pytest.mark.usefixtures("fresh_db")
def test_opposing_concurrent_transfers_do_not_deadlock_or_lose_money() -> None:
    alice = db.create_account("Alice").id
    bob = db.create_account("Bob").id
    fund(alice, 500, "seed-a")
    fund(bob, 500, "seed-b")

    def transfer(i: int) -> None:
        source, destination = (alice, bob) if i % 2 else (bob, alice)
        with contextlib.suppress(db.InsufficientFundsError):
            db.apply_transaction(
                idempotency_key=f"t-{i}",
                transaction_type="transfer",
                amount=3,
                source_id=source,
                destination_id=destination,
            )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(transfer, range(80)))

    assert balance(alice) + balance(bob) == 1000


# --- the database refuses bad states even if application code is buggy ------ #
def test_database_rejects_a_negative_balance(fresh_db: Path) -> None:
    alice = db.create_account("Alice").id

    with db.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE accounts SET balance = -1 WHERE id = ?", (alice,))


def test_database_rejects_a_non_positive_amount(fresh_db: Path) -> None:
    alice = db.create_account("Alice").id

    with db.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (account_destination, idempotency_key, amount, "
            "transaction_type, timestamp) VALUES (?, 'k', 0, 'deposit', 'now')",
            (alice,),
        )


def test_database_rejects_a_duplicate_idempotency_key(fresh_db: Path) -> None:
    alice = db.create_account("Alice").id
    insert = (
        "INSERT INTO transactions (account_destination, idempotency_key, amount, "
        "transaction_type, timestamp) VALUES (?, 'dup', 1, 'deposit', 'now')"
    )

    with db.connection() as conn:
        conn.execute(insert, (alice,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (alice,))


def test_database_rejects_a_transaction_for_an_unknown_account(fresh_db: Path) -> None:
    with db.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (account_destination, idempotency_key, amount, "
            "transaction_type, timestamp) VALUES (999, 'k', 1, 'deposit', 'now')"
        )
