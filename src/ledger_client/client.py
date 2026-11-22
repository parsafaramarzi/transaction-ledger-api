"""Synchronous and asynchronous clients for the ledger API."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from ledger_client.models import (
    Account,
    LedgerAPIError,
    Transaction,
    TransactionResult,
    account_from_json,
    raise_for_error,
    transaction_from_json,
)
from ledger_client.retry import async_retry_on_busy, retry_on_busy

DEFAULT_URL = "http://localhost:8000"


def _transaction_body(
    transaction_type: str, amount: int, source_id: int | None, destination_id: int | None
) -> dict[str, Any]:
    return {
        "transaction_type": transaction_type,
        "amount": amount,
        "account_source_id": source_id,
        "account_destination_id": destination_id,
    }


def _list_params(account_id: int | None, page_size: int, cursor: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": page_size}
    if account_id is not None:
        params["account_id"] = account_id
    if cursor is not None:
        params["cursor"] = cursor
    return params


# --------------------------------------------------------------------------- #
# Sync                                                                        #
# --------------------------------------------------------------------------- #
class LedgerClient:
    """Blocking client. Pass ``client=`` to reuse an existing ``httpx.Client`` (e.g. in tests).

    Retries are applied only where repeating a request is safe:

    * ``create_transaction`` carries an idempotency key, so a retry cannot apply it twice;
    * ``create_account`` has no key, so it is deliberately *not* retried.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        retry_attempts: int = 3,
        retry_backoff: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._owns_client = client is None
        self._http = client or httpx.Client(base_url=base_url, timeout=timeout)
        # The decorator is applied per instance so retry settings and the sleep function are
        # configurable (and fakeable in tests).
        self._send_transaction = retry_on_busy(
            attempts=retry_attempts, backoff=retry_backoff, sleep=sleep
        )(self._post_transaction)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> LedgerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- accounts ----------------------------------------------------------- #
    def create_account(self, name: str) -> Account:
        response = self._http.post("/accounts", json={"name": name})
        raise_for_error(response)
        return account_from_json(response.json()["account"])

    def get_account(self, account_id: int) -> Account:
        response = self._http.get(f"/accounts/{account_id}")
        raise_for_error(response)
        return account_from_json(response.json())

    # -- transactions ------------------------------------------------------- #
    def create_transaction(
        self,
        transaction_type: str,
        amount: int,
        *,
        source_id: int | None = None,
        destination_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> TransactionResult:
        # Chosen ONCE, here, outside the retried call: every retry reuses the same key.
        key = idempotency_key or str(uuid.uuid4())
        return self._send_transaction(key, transaction_type, amount, source_id, destination_id)

    def _post_transaction(
        self,
        key: str,
        transaction_type: str,
        amount: int,
        source_id: int | None,
        destination_id: int | None,
    ) -> TransactionResult:
        response = self._http.post(
            "/transactions",
            json=_transaction_body(transaction_type, amount, source_id, destination_id),
            headers={"Idempotency-Key": key},
        )
        raise_for_error(response)
        return TransactionResult(
            transaction=transaction_from_json(response.json()["transaction"]),
            created=response.status_code == 201,
        )

    def get_transaction(self, transaction_id: int) -> Transaction:
        response = self._http.get(f"/transactions/{transaction_id}")
        raise_for_error(response)
        return transaction_from_json(response.json())

    def iter_transactions(
        self, account_id: int | None = None, *, page_size: int = 50
    ) -> Iterator[Transaction]:
        """Yield every transaction, newest first, fetching one page at a time.

        This is a generator: nothing is requested until you start iterating, and only one
        page is held in memory however long the history is.
        """
        cursor: int | None = None
        while True:
            response = self._http.get(
                "/transactions", params=_list_params(account_id, page_size, cursor)
            )
            raise_for_error(response)
            page = response.json()
            for item in page["items"]:
                yield transaction_from_json(item)
            cursor = page["next_cursor"]
            if cursor is None:
                return


# --------------------------------------------------------------------------- #
# Async                                                                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TransferRequest:
    """One transfer in a bulk run. The caller supplies the key, so re-running is safe."""

    source_id: int
    destination_id: int
    amount: int
    idempotency_key: str


@dataclass(frozen=True)
class BulkOutcome:
    request: TransferRequest
    result: TransactionResult | None
    error: LedgerAPIError | None

    @property
    def ok(self) -> bool:
        return self.error is None


class AsyncLedgerClient:
    """The ``async`` client. Same shape as :class:`LedgerClient`, plus :meth:`bulk_transfer`."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        retry_attempts: int = 3,
        retry_backoff: float = 0.1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._send_transaction = async_retry_on_busy(
            attempts=retry_attempts, backoff=retry_backoff, sleep=sleep
        )(self._post_transaction)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncLedgerClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def create_account(self, name: str) -> Account:
        response = await self._http.post("/accounts", json={"name": name})
        raise_for_error(response)
        return account_from_json(response.json()["account"])

    async def get_account(self, account_id: int) -> Account:
        response = await self._http.get(f"/accounts/{account_id}")
        raise_for_error(response)
        return account_from_json(response.json())

    async def create_transaction(
        self,
        transaction_type: str,
        amount: int,
        *,
        source_id: int | None = None,
        destination_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> TransactionResult:
        key = idempotency_key or str(uuid.uuid4())
        return await self._send_transaction(
            key, transaction_type, amount, source_id, destination_id
        )

    async def _post_transaction(
        self,
        key: str,
        transaction_type: str,
        amount: int,
        source_id: int | None,
        destination_id: int | None,
    ) -> TransactionResult:
        response = await self._http.post(
            "/transactions",
            json=_transaction_body(transaction_type, amount, source_id, destination_id),
            headers={"Idempotency-Key": key},
        )
        raise_for_error(response)
        return TransactionResult(
            transaction=transaction_from_json(response.json()["transaction"]),
            created=response.status_code == 201,
        )

    async def aiter_transactions(
        self, account_id: int | None = None, *, page_size: int = 50
    ) -> AsyncIterator[Transaction]:
        """Async generator twin of :meth:`LedgerClient.iter_transactions`."""
        cursor: int | None = None
        while True:
            response = await self._http.get(
                "/transactions", params=_list_params(account_id, page_size, cursor)
            )
            raise_for_error(response)
            page = response.json()
            for item in page["items"]:
                yield transaction_from_json(item)
            cursor = page["next_cursor"]
            if cursor is None:
                return

    async def bulk_transfer(
        self, requests: Sequence[TransferRequest], *, concurrency: int = 10
    ) -> list[BulkOutcome]:
        """Run many transfers concurrently, at most ``concurrency`` in flight at once.

        Results come back in the same order as ``requests``. A failed transfer does not stop
        the others; it is reported in its :class:`BulkOutcome`. Because every request has its
        own idempotency key, running the same batch again applies nothing a second time.
        """
        gate = asyncio.Semaphore(concurrency)

        async def run_one(request: TransferRequest) -> BulkOutcome:
            async with gate:
                try:
                    result = await self.create_transaction(
                        "transfer",
                        request.amount,
                        source_id=request.source_id,
                        destination_id=request.destination_id,
                        idempotency_key=request.idempotency_key,
                    )
                except LedgerAPIError as exc:
                    return BulkOutcome(request=request, result=None, error=exc)
                return BulkOutcome(request=request, result=result, error=None)

        return list(await asyncio.gather(*(run_one(r) for r in requests)))
