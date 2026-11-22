"""SQLite persistence and the ledger's business rules.

Money is stored as integer minor units (e.g. cents). Never floats.

Every money movement goes through :func:`apply_transaction`, which does the
idempotency lookup, the balance check, the balance updates and the transaction
insert inside ONE ``BEGIN IMMEDIATE`` transaction. That is what makes it safe:

* ``BEGIN IMMEDIATE`` takes SQLite's write lock up front, so two concurrent
  requests can never both read the same balance and both spend it
  (no check-then-update race), and two concurrent requests with the same
  idempotency key are serialised, so the second one sees the first one's row.
* If anything fails part-way (insufficient funds, a crash, an exception), the
  whole transaction is rolled back: no half-applied transfer, and no money
  moved without a matching transaction row.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

TransactionType = Literal["deposit", "withdraw", "transfer"]

# Read at call time (not bound into functions) so tests can monkeypatch them.
DB_PATH: str = os.environ.get("LEDGER_DB_PATH", "transaction_ledger.db")
BUSY_TIMEOUT: float = float(os.environ.get("LEDGER_DB_TIMEOUT", "10"))  # seconds to wait for a lock

# SQLite integers are signed 64-bit. Anything beyond that is silently turned into a
# float (balances) or raises OverflowError (bound parameters), so we bound everything.
MAX_INT64 = 2**63 - 1
MAX_BALANCE = MAX_INT64
MAX_AMOUNT = 10**12  # per transaction, in minor units (10 billion major units)


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #
class LedgerError(Exception):
    """Base class for every business-rule error raised by this module."""


class AccountNotFoundError(LedgerError):
    """A referenced account does not exist."""


class InsufficientFundsError(LedgerError):
    """The source account does not hold enough money."""


class InvalidTransactionError(LedgerError):
    """The transaction's shape is invalid (e.g. a deposit with a source)."""


class IdempotencyConflictError(LedgerError):
    """An idempotency key was reused with a different request body."""


class BalanceLimitExceededError(LedgerError):
    """Crediting the destination would push its balance past ``MAX_BALANCE``."""


class DatabaseBusyError(LedgerError):
    """Another writer held the database lock for longer than ``BUSY_TIMEOUT``.

    Safe to retry: nothing was applied.
    """


# --------------------------------------------------------------------------- #
# Data                                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Account:
    id: int
    name: str
    balance: int


@dataclass(frozen=True)
class Transaction:
    id: int
    account_source: int | None
    account_destination: int | None
    idempotency_key: str
    amount: int
    transaction_type: TransactionType
    timestamp: str


# --------------------------------------------------------------------------- #
# Connections and schema                                                      #
# --------------------------------------------------------------------------- #
def get_connection() -> sqlite3.Connection:
    """Open a new connection. One per operation; never share across threads."""
    # isolation_level=None -> autocommit; transactions are managed explicitly.
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create the tables if they do not exist. Call once at app startup."""
    with connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT    NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0
                    CHECK (typeof(balance) = 'integer' AND balance >= 0)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                account_source      INTEGER REFERENCES accounts (id),
                account_destination INTEGER REFERENCES accounts (id),
                idempotency_key     TEXT    NOT NULL UNIQUE,
                amount              INTEGER NOT NULL CHECK (amount > 0),
                transaction_type    TEXT    NOT NULL
                    CHECK (transaction_type IN ('deposit', 'withdraw', 'transfer')),
                timestamp           TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_source "
            "ON transactions (account_source, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_destination "
            "ON transactions (account_destination, id)"
        )


# --------------------------------------------------------------------------- #
# Row mapping                                                                 #
# --------------------------------------------------------------------------- #
def _account_from_row(row: sqlite3.Row) -> Account:
    return Account(id=row["id"], name=row["name"], balance=row["balance"])


def _transaction_from_row(row: sqlite3.Row) -> Transaction:
    return Transaction(
        id=row["id"],
        account_source=row["account_source"],
        account_destination=row["account_destination"],
        idempotency_key=row["idempotency_key"],
        amount=row["amount"],
        transaction_type=row["transaction_type"],
        timestamp=row["timestamp"],
    )


# --------------------------------------------------------------------------- #
# Accounts                                                                    #
# --------------------------------------------------------------------------- #
def create_account(name: str) -> Account:
    """Create an account. The server assigns the id; the balance starts at 0."""
    with connection() as conn:
        try:
            cur = conn.execute("INSERT INTO accounts (name, balance) VALUES (?, 0)", (name,))
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise DatabaseBusyError("The database is busy, retry shortly") from exc
            raise
        return Account(id=int(cur.lastrowid or 0), name=name, balance=0)


def get_account(account_id: int) -> Account | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return _account_from_row(row) if row else None


# --------------------------------------------------------------------------- #
# Transactions                                                                #
# --------------------------------------------------------------------------- #
def _validate_shape(
    transaction_type: TransactionType,
    amount: int,
    source_id: int | None,
    destination_id: int | None,
) -> None:
    if amount <= 0:
        raise InvalidTransactionError("Amount must be positive")
    if amount > MAX_AMOUNT:
        raise InvalidTransactionError(f"Amount must not exceed {MAX_AMOUNT}")
    for account_id in (source_id, destination_id):
        if account_id is not None and not 1 <= account_id <= MAX_INT64:
            raise InvalidTransactionError("Account id is out of range")
    if transaction_type == "deposit":
        if source_id is not None or destination_id is None:
            raise InvalidTransactionError("A deposit needs a destination account and no source")
    elif transaction_type == "withdraw":
        if source_id is None or destination_id is not None:
            raise InvalidTransactionError("A withdrawal needs a source account and no destination")
    else:  # transfer
        if source_id is None or destination_id is None:
            raise InvalidTransactionError("A transfer needs both a source and a destination")
        if source_id == destination_id:
            raise InvalidTransactionError("A transfer needs two different accounts")


def _find_by_key(conn: sqlite3.Connection, idempotency_key: str) -> Transaction | None:
    row = conn.execute(
        "SELECT * FROM transactions WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return _transaction_from_row(row) if row else None


def _require_account(conn: sqlite3.Connection, account_id: int | None, message: str) -> None:
    if account_id is None:
        return
    row = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise AccountNotFoundError(message)


def _insert_transaction(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    transaction_type: TransactionType,
    amount: int,
    source_id: int | None,
    destination_id: int | None,
) -> Transaction:
    timestamp = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO transactions "
        "(account_source, account_destination, idempotency_key, amount, transaction_type, "
        "timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, destination_id, idempotency_key, amount, transaction_type, timestamp),
    )
    return Transaction(
        id=int(cur.lastrowid or 0),
        account_source=source_id,
        account_destination=destination_id,
        idempotency_key=idempotency_key,
        amount=amount,
        transaction_type=transaction_type,
        timestamp=timestamp,
    )


def apply_transaction(
    *,
    idempotency_key: str,
    transaction_type: TransactionType,
    amount: int,
    source_id: int | None,
    destination_id: int | None,
) -> tuple[Transaction, bool]:
    """Apply a transaction exactly once.

    Returns ``(transaction, created)``. ``created`` is False when the idempotency
    key was already used by an identical request: the original transaction is
    returned and nothing is applied a second time.

    Raises:
        InvalidTransactionError: the request has the wrong shape.
        AccountNotFoundError: the source or destination account does not exist.
        InsufficientFundsError: the source account cannot cover the amount.
        IdempotencyConflictError: the key was used before with a different body.
        BalanceLimitExceededError: the credit would overflow the destination balance.
        DatabaseBusyError: the write lock was not available in time; safe to retry.
    """
    _validate_shape(transaction_type, amount, source_id, destination_id)

    conn = get_connection()
    try:
        # Write lock up front: the idempotency check, balance check, balance
        # updates and insert below cannot interleave with another writer.
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise DatabaseBusyError("The database is busy, retry shortly") from exc
            raise

        existing = _find_by_key(conn, idempotency_key)
        if existing is not None:
            same_request = (
                existing.transaction_type == transaction_type
                and existing.amount == amount
                and existing.account_source == source_id
                and existing.account_destination == destination_id
            )
            if not same_request:
                raise IdempotencyConflictError(
                    "Idempotency key was already used with a different request"
                )
            return existing, False

        _require_account(conn, source_id, "Source account not found")
        _require_account(conn, destination_id, "Destination account not found")

        if source_id is not None:
            # Check and debit in one statement: only succeeds if funds suffice.
            cur = conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE id = ? AND balance >= ?",
                (amount, source_id, amount),
            )
            if cur.rowcount == 0:
                raise InsufficientFundsError("Insufficient funds for the transaction")
        if destination_id is not None:
            # Same idea for the credit: refuse rather than overflow int64. The account
            # exists (checked above), so a miss here can only mean "would exceed the limit".
            cur = conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE id = ? AND balance <= ?",
                (amount, destination_id, MAX_BALANCE - amount),
            )
            if cur.rowcount == 0:
                raise BalanceLimitExceededError("Destination balance would exceed the maximum")

        transaction = _insert_transaction(
            conn,
            idempotency_key=idempotency_key,
            transaction_type=transaction_type,
            amount=amount,
            source_id=source_id,
            destination_id=destination_id,
        )
        conn.execute("COMMIT")
        return transaction, True
    finally:
        # No-op after a successful COMMIT; undoes everything on any failure.
        conn.rollback()
        conn.close()


def get_transaction(transaction_id: int) -> Transaction | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    return _transaction_from_row(row) if row else None


def list_transactions(
    *,
    account_id: int | None = None,
    limit: int = 10,
    cursor: int | None = None,
) -> tuple[list[Transaction], int | None]:
    """Newest-first page of transactions, optionally for one account.

    ``cursor`` is the id of the last transaction of the previous page; pass the
    returned ``next_cursor`` to get the following page. ``next_cursor`` is None
    on the last page.
    """
    clauses: list[str] = []
    params: list[int] = []
    if account_id is not None:
        clauses.append("(account_source = ? OR account_destination = ?)")
        params += [account_id, account_id]
    if cursor is not None:
        clauses.append("id < ?")
        params.append(cursor)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with connection() as conn:
        rows = conn.execute(
            # `where` is assembled only from the fixed fragments above; values are bound.
            f"SELECT * FROM transactions {where} ORDER BY id DESC LIMIT ?",
            (*params, limit + 1),  # one extra row tells us whether another page exists
        ).fetchall()

    page = [_transaction_from_row(row) for row in rows[:limit]]
    next_cursor = page[-1].id if len(rows) > limit else None
    return page, next_cursor
