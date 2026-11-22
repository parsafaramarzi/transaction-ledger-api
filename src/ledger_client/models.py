"""Plain data types and errors for the ledger client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    balance: int


@dataclass(frozen=True)
class Transaction:
    id: int
    account_source_id: int | None
    account_destination_id: int | None
    amount: int
    transaction_type: str
    timestamp: str


@dataclass(frozen=True)
class TransactionResult:
    transaction: Transaction
    created: bool  # False when the server recognised the idempotency key and replayed it


class LedgerAPIError(Exception):
    """The API answered with an error status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class LedgerBusyError(LedgerAPIError):
    """503: the ledger was busy and applied nothing. Safe to retry with the same key."""

    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        super().__init__(503, detail)
        self.retry_after = retry_after


def account_from_json(data: dict[str, Any]) -> Account:
    return Account(id=data["id"], name=data["name"], balance=data["balance"])


def transaction_from_json(data: dict[str, Any]) -> Transaction:
    return Transaction(
        id=data["id"],
        account_source_id=data["account_source_id"],
        account_destination_id=data["account_destination_id"],
        amount=data["amount"],
        transaction_type=data["transaction_type"],
        timestamp=data["timestamp"],
    )


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except (ValueError, AttributeError):
        return response.text
    if isinstance(detail, list):  # FastAPI validation errors are a list of objects
        return "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', []))}: {item.get('msg', '')}"
            for item in detail
        )
    return str(detail)


def raise_for_error(response: httpx.Response) -> None:
    """Turn an error response into a typed exception (no-op for 2xx/3xx)."""
    if response.status_code < 400:
        return
    detail = _detail(response)
    if response.status_code == 503:
        header = response.headers.get("retry-after")
        try:
            retry_after = float(header) if header is not None else None
        except ValueError:
            retry_after = None
        raise LedgerBusyError(detail, retry_after)
    raise LedgerAPIError(response.status_code, detail)
