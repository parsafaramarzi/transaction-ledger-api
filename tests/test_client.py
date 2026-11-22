"""Tests for ``ledger_client``: the retry decorators, the lazy generator, and the real API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import BalanceOf, MakeAccount

from ledger import db
from ledger.api import app
from ledger_client import (
    AsyncLedgerClient,
    LedgerAPIError,
    LedgerBusyError,
    LedgerClient,
    TransferRequest,
    async_retry_on_busy,
    retry_on_busy,
)

Handler = Callable[[httpx.Request], httpx.Response]


def tx_json(tx_id: int = 1, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": tx_id,
        "account_source_id": None,
        "account_destination_id": 1,
        "amount": 100,
        "transaction_type": "deposit",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    return {**body, **overrides}


def busy() -> httpx.Response:
    return httpx.Response(503, json={"detail": "busy"}, headers={"Retry-After": "1"})


def made(handler: Handler) -> tuple[LedgerClient, list[float]]:
    """A client wired to ``handler`` whose sleeps are recorded instead of waited for."""
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ledger.test")
    return LedgerClient(client=http, sleep=sleeps.append, retry_backoff=0.1), sleeps


# --------------------------------------------------------------------------- #
# The decorators                                                              #
# --------------------------------------------------------------------------- #
def test_retry_on_busy_retries_then_returns_the_result() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    @retry_on_busy(attempts=3, backoff=0.1, sleep=sleeps.append)
    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise LedgerBusyError("busy")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]  # exponential backoff


def test_retry_on_busy_never_sleeps_less_than_the_servers_retry_after() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    @retry_on_busy(attempts=2, backoff=0.1, sleep=sleeps.append)
    def flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise LedgerBusyError("busy", retry_after=5.0)
        return "ok"

    flaky()

    assert sleeps == [5.0]


def test_retry_on_busy_gives_up_after_the_last_attempt_and_reraises() -> None:
    calls: list[int] = []

    @retry_on_busy(attempts=3, sleep=lambda _s: None)
    def always_busy() -> None:
        calls.append(1)
        raise LedgerBusyError("busy")

    with pytest.raises(LedgerBusyError):
        always_busy()
    assert len(calls) == 3


def test_retry_on_busy_does_not_retry_other_errors() -> None:
    calls: list[int] = []

    @retry_on_busy(attempts=5, sleep=lambda _s: None)
    def not_found() -> None:
        calls.append(1)
        raise LedgerAPIError(404, "nope")

    with pytest.raises(LedgerAPIError):
        not_found()
    assert len(calls) == 1


def test_retry_on_busy_preserves_the_wrapped_functions_metadata() -> None:
    @retry_on_busy()
    def documented(x: int) -> int:
        """Doubles x."""
        return x * 2

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Doubles x."
    assert documented(4) == 8  # arguments pass straight through


@pytest.mark.parametrize("attempts", [0, -1])
def test_retry_decorators_reject_fewer_than_one_attempt(attempts: int) -> None:
    with pytest.raises(ValueError):
        retry_on_busy(attempts=attempts)
    with pytest.raises(ValueError):
        async_retry_on_busy(attempts=attempts)


def test_async_retry_on_busy_retries_then_returns_the_result() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    @async_retry_on_busy(attempts=3, backoff=0.1, sleep=fake_sleep)
    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise LedgerBusyError("busy")
        return "ok"

    assert asyncio.run(flaky()) == "ok"
    assert sleeps == [0.1, 0.2]


# --------------------------------------------------------------------------- #
# The sync client against a scripted server                                   #
# --------------------------------------------------------------------------- #
def test_every_retry_reuses_the_same_idempotency_key() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        if len(keys) < 3:
            return busy()
        return httpx.Response(201, json={"message": "ok", "transaction": tx_json()})

    client, sleeps = made(handler)

    result = client.create_transaction("deposit", 100, destination_id=1)

    assert result.created is True
    assert len(keys) == 3
    assert len(set(keys)) == 1  # the whole point: a retry must not look like a new request
    assert sleeps == [1.0, 1.0]  # the server's Retry-After (1s) beat the 0.1s/0.2s backoff


def test_separate_calls_get_separate_keys_and_an_explicit_key_is_respected() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(201, json={"message": "ok", "transaction": tx_json()})

    client, _ = made(handler)
    client.create_transaction("deposit", 1, destination_id=1)
    client.create_transaction("deposit", 1, destination_id=1)
    client.create_transaction("deposit", 1, destination_id=1, idempotency_key="mine")

    assert keys[0] != keys[1]
    assert keys[2] == "mine"


def test_a_replay_is_reported_as_not_created() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "replayed", "transaction": tx_json(7)})

    client, _ = made(handler)

    result = client.create_transaction("deposit", 100, destination_id=1, idempotency_key="k")

    assert result.created is False
    assert result.transaction.id == 7


def test_request_body_matches_the_api_contract() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"message": "ok", "transaction": tx_json()})

    client, _ = made(handler)
    client.create_transaction("transfer", 25, source_id=1, destination_id=2)

    assert seen == [
        {
            "transaction_type": "transfer",
            "amount": 25,
            "account_source_id": 1,
            "account_destination_id": 2,
        }
    ]


def test_errors_become_typed_exceptions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/9"):
            return httpx.Response(404, json={"detail": "Account not found"})
        return httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "amount"], "msg": "must be positive"}]},
        )

    client, _ = made(handler)

    with pytest.raises(LedgerAPIError) as missing:
        client.get_account(9)
    assert missing.value.status_code == 404
    assert missing.value.detail == "Account not found"

    with pytest.raises(LedgerAPIError) as invalid:
        client.create_transaction("deposit", -1, destination_id=1)
    assert invalid.value.status_code == 422
    assert "body.amount: must be positive" in invalid.value.detail


def test_a_503_that_never_clears_raises_busy_after_all_attempts() -> None:
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return busy()

    client, _ = made(handler)

    with pytest.raises(LedgerBusyError) as exc:
        client.create_transaction("deposit", 100, destination_id=1)
    assert exc.value.retry_after == 1.0
    assert len(attempts) == 3


def test_create_account_is_deliberately_not_retried() -> None:
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return busy()

    client, _ = made(handler)

    with pytest.raises(LedgerBusyError):
        client.create_account("Alice")
    assert len(attempts) == 1  # no idempotency key, so a blind retry could create two accounts


# --------------------------------------------------------------------------- #
# The generator                                                               #
# --------------------------------------------------------------------------- #
def paging_handler(requests_seen: list[httpx.Request]) -> Handler:
    """A server holding 5 transactions (ids 5..1), served 2 per page."""
    ids = [5, 4, 3, 2, 1]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        limit = int(request.url.params["limit"])
        cursor = request.url.params.get("cursor")
        remaining = [i for i in ids if cursor is None or i < int(cursor)]
        page = remaining[:limit]
        next_cursor = page[-1] if len(remaining) > limit else None
        return httpx.Response(
            200, json={"items": [tx_json(i) for i in page], "next_cursor": next_cursor}
        )

    return handler


def test_iter_transactions_is_lazy() -> None:
    seen: list[httpx.Request] = []
    client, _ = made(paging_handler(seen))

    stream = client.iter_transactions(page_size=2)
    assert seen == []  # creating the generator requested nothing

    assert next(stream).id == 5
    assert len(seen) == 1  # one page fetched, not all three
    assert next(stream).id == 4
    assert len(seen) == 1  # still the same page
    assert next(stream).id == 3
    assert len(seen) == 2  # the next page is fetched only when needed


def test_iter_transactions_follows_the_cursor_to_the_end() -> None:
    seen: list[httpx.Request] = []
    client, _ = made(paging_handler(seen))

    ids = [t.id for t in client.iter_transactions(page_size=2)]

    assert ids == [5, 4, 3, 2, 1]
    assert [r.url.params.get("cursor") for r in seen] == [None, "4", "2"]


def test_iter_transactions_can_stop_early_without_fetching_the_rest() -> None:
    seen: list[httpx.Request] = []
    client, _ = made(paging_handler(seen))

    first_two = [
        t.id for _, t in zip(range(2), client.iter_transactions(page_size=2), strict=False)
    ]

    assert first_two == [5, 4]
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# Against the real app                                                        #
# --------------------------------------------------------------------------- #
def test_sync_client_end_to_end(client: TestClient) -> None:
    api = LedgerClient(client=client)
    alice = api.create_account("Alice")
    bob = api.create_account("Bob")
    assert alice.balance == 0

    api.create_transaction("deposit", 1000, destination_id=alice.id)
    moved = api.create_transaction("transfer", 250, source_id=alice.id, destination_id=bob.id)

    assert moved.created is True
    assert api.get_account(alice.id).balance == 750
    assert api.get_account(bob.id).balance == 250
    assert api.get_transaction(moved.transaction.id) == moved.transaction
    assert [t.transaction_type for t in api.iter_transactions(alice.id)] == ["transfer", "deposit"]
    with pytest.raises(LedgerAPIError) as insufficient:
        api.create_transaction("withdraw", 10**6, source_id=alice.id)
    assert insufficient.value.status_code == 422


def test_iter_transactions_walks_many_pages_of_the_real_api(
    client: TestClient, make_account: MakeAccount
) -> None:
    api = LedgerClient(client=client)
    alice = make_account("Alice")
    for i in range(25):
        api.create_transaction("deposit", 1, destination_id=alice, idempotency_key=f"d-{i}")

    ids = [t.id for t in api.iter_transactions(alice, page_size=10)]

    assert len(ids) == 25
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 25


def test_a_real_503_is_retried_with_the_same_key_and_applies_once(
    client: TestClient,
    make_account: MakeAccount,
    balance_of: BalanceOf,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = make_account("Alice", 100)
    monkeypatch.setattr(db, "BUSY_TIMEOUT", 0.2)
    blocker = db.get_connection()
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the lock...
    sleeps: list[float] = []

    def release_the_lock(seconds: float) -> None:  # ...until the client's first backoff sleep
        sleeps.append(seconds)
        blocker.rollback()

    api = LedgerClient(client=client, sleep=release_the_lock)
    try:
        result = api.create_transaction("deposit", 50, destination_id=alice)
    finally:
        blocker.rollback()
        blocker.close()

    assert len(sleeps) == 1  # exactly one 503, so exactly one backoff
    assert result.created is True  # the first attempt got a 503, the retry succeeded
    assert balance_of(alice) == 150  # ...and it was applied exactly once


def test_bulk_transfer_runs_concurrently_conserves_money_and_is_safe_to_rerun(
    fresh_db: object,
) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            api = AsyncLedgerClient(client=http)
            alice = await api.create_account("Alice")
            bob = await api.create_account("Bob")
            await api.create_transaction("deposit", 1000, destination_id=alice.id)
            await api.create_transaction("deposit", 1000, destination_id=bob.id)

            batch = [
                TransferRequest(
                    source_id=alice.id if i % 2 else bob.id,
                    destination_id=bob.id if i % 2 else alice.id,
                    amount=7,
                    idempotency_key=f"bulk-{i}",
                )
                for i in range(30)
            ]

            first = await api.bulk_transfer(batch, concurrency=10)
            after_first = (
                (await api.get_account(alice.id)).balance,
                (await api.get_account(bob.id)).balance,
            )

            assert [o.request for o in first] == batch  # same order as requested
            assert all(o.ok and o.result and o.result.created for o in first)
            assert sum(after_first) == 2000  # no money created or lost under concurrency

            again = await api.bulk_transfer(batch, concurrency=10)  # same keys: a re-run
            after_again = (
                (await api.get_account(alice.id)).balance,
                (await api.get_account(bob.id)).balance,
            )

            assert all(o.ok and o.result and not o.result.created for o in again)
            assert after_again == after_first  # nothing was applied a second time

    asyncio.run(scenario())


def test_bulk_transfer_never_exceeds_the_concurrency_limit_but_does_run_in_parallel() -> None:
    in_flight = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)  # hold the "request" open so overlaps are observable
        in_flight -= 1
        return httpx.Response(201, json={"message": "ok", "transaction": tx_json()})

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")
        async with http:
            api = AsyncLedgerClient(client=http)
            batch = [TransferRequest(1, 2, 1, f"k-{i}") for i in range(30)]
            outcomes = await api.bulk_transfer(batch, concurrency=4)
            assert len(outcomes) == 30 and all(o.ok for o in outcomes)

    asyncio.run(scenario())

    assert 1 < peak <= 4  # genuinely concurrent, yet never more than asked for


def test_bulk_transfer_reports_failures_without_stopping_the_rest(fresh_db: object) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            api = AsyncLedgerClient(client=http)
            rich = await api.create_account("Rich")
            poor = await api.create_account("Poor")
            await api.create_transaction("deposit", 100, destination_id=rich.id)

            outcomes = await api.bulk_transfer(
                [
                    TransferRequest(rich.id, poor.id, 10, "ok-1"),
                    TransferRequest(poor.id, rich.id, 5000, "too-much"),  # poor has little
                    TransferRequest(rich.id, poor.id, 10, "ok-2"),
                ]
            )

            assert [o.ok for o in outcomes] == [True, False, True]
            failed = outcomes[1].error
            assert failed is not None and failed.status_code == 422
            assert (await api.get_account(poor.id)).balance == 20

    asyncio.run(scenario())


def test_async_generator_yields_every_transaction(fresh_db: object) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            api = AsyncLedgerClient(client=http)
            alice = await api.create_account("Alice")
            for i in range(7):
                await api.create_transaction(
                    "deposit", 1, destination_id=alice.id, idempotency_key=f"a-{i}"
                )

            ids = [t.id async for t in api.aiter_transactions(alice.id, page_size=3)]

            assert len(ids) == 7
            assert ids == sorted(ids, reverse=True)

    asyncio.run(scenario())
