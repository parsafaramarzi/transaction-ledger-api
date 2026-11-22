from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from helpers import BalanceOf, MakeAccount, PostTransaction


def history(client: TestClient, account_id: int | None = None) -> list[dict[str, object]]:
    """All transactions (newest first), read through the API."""
    suffix = f"&account_id={account_id}" if account_id is not None else ""
    items: list[dict[str, object]] = client.get(f"/transactions/?limit=100{suffix}").json()["items"]
    return items


# --------------------------------------------------------------------------- #
# Accounts                                                                    #
# --------------------------------------------------------------------------- #
def test_create_account_returns_201_and_starts_at_zero(client: TestClient) -> None:
    response = client.post("/accounts/", json={"name": "Alice"})

    assert response.status_code == 201
    account = response.json()["account"]
    assert account["name"] == "Alice"
    assert account["balance"] == 0
    assert isinstance(account["id"], int)


def test_create_account_ignores_a_client_supplied_balance(client: TestClient) -> None:
    response = client.post("/accounts/", json={"name": "Alice", "balance": 1_000_000})

    assert response.json()["account"]["balance"] == 0


def test_create_account_assigns_distinct_ids(client: TestClient) -> None:
    first = client.post("/accounts/", json={"name": "A"}).json()["account"]["id"]
    second = client.post("/accounts/", json={"name": "B"}).json()["account"]["id"]

    assert first != second


@pytest.mark.parametrize("payload", [{}, {"name": ""}])
def test_create_account_rejects_missing_or_empty_name(
    client: TestClient, payload: dict[str, object]
) -> None:
    assert client.post("/accounts/", json=payload).status_code == 422


def test_get_account_returns_its_fields(client: TestClient, make_account: MakeAccount) -> None:
    account_id = make_account("Alice", 1000)

    body = client.get(f"/accounts/{account_id}").json()

    assert body == {"id": account_id, "name": "Alice", "balance": 1000}


def test_get_nonexistent_account_returns_404(client: TestClient) -> None:
    assert client.get("/accounts/999").status_code == 404


# --------------------------------------------------------------------------- #
# Happy paths                                                                 #
# --------------------------------------------------------------------------- #
def test_deposit_increases_balance(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)

    response = post_transaction("deposit", 200, destination=alice)

    assert response.status_code == 201
    assert balance_of(alice) == 1200


def test_withdraw_decreases_balance(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)

    response = post_transaction("withdraw", 300, source=alice)

    assert response.status_code == 201
    assert balance_of(alice) == 700


def test_transfer_updates_both_accounts(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)
    bob = make_account("Bob", 500)

    response = post_transaction("transfer", 300, source=alice, destination=bob)

    assert response.status_code == 201
    assert balance_of(alice) == 700
    assert balance_of(bob) == 800


def test_withdrawing_the_exact_balance_is_allowed(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 100)

    assert post_transaction("withdraw", 100, source=alice).status_code == 201
    assert balance_of(alice) == 0


# --------------------------------------------------------------------------- #
# Rejected transactions leave no trace                                        #
# --------------------------------------------------------------------------- #
def test_withdraw_insufficient_funds_returns_422_and_changes_nothing(
    client: TestClient,
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    balance_of: BalanceOf,
) -> None:
    alice = make_account("Alice", 100)

    response = post_transaction("withdraw", 9999, source=alice)

    assert response.status_code == 422
    assert balance_of(alice) == 100
    assert [t["transaction_type"] for t in history(client, alice)] == ["deposit"]  # only the seed


def test_transfer_insufficient_funds_returns_422_and_changes_nothing(
    client: TestClient,
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    balance_of: BalanceOf,
) -> None:
    alice = make_account("Alice", 100)
    bob = make_account("Bob", 500)

    response = post_transaction("transfer", 9999, source=alice, destination=bob)

    assert response.status_code == 422
    assert balance_of(alice) == 100
    assert balance_of(bob) == 500
    assert len(history(client, bob)) == 1  # only Bob's seed deposit


def test_source_account_not_found_returns_404(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    bob = make_account("Bob", 500)

    response = post_transaction("transfer", 100, source=999, destination=bob)

    assert response.status_code == 404
    assert len(history(client, bob)) == 1


def test_destination_account_not_found_returns_404(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)

    response = post_transaction("transfer", 100, source=alice, destination=999)

    assert response.status_code == 404
    assert balance_of(alice) == 1000  # the debit was not left applied


def test_unrecognized_transaction_type_returns_422_and_persists_nothing(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice", 1000)

    response = post_transaction("yeet", 200, destination=alice)

    assert response.status_code == 422
    assert len(history(client, alice)) == 1


@pytest.mark.parametrize("amount", [0, -50])
def test_non_positive_amount_is_rejected(
    make_account: MakeAccount, post_transaction: PostTransaction, amount: int
) -> None:
    alice = make_account("Alice", 1000)

    assert post_transaction("deposit", amount, destination=alice).status_code == 422
    assert post_transaction("withdraw", amount, source=alice).status_code == 422


def test_a_negative_deposit_cannot_be_used_to_drain_an_account(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)

    post_transaction("deposit", -1000, destination=alice)

    assert balance_of(alice) == 1000


def test_float_amount_is_rejected(client: TestClient, make_account: MakeAccount) -> None:
    alice = make_account("Alice", 1000)

    response = client.post(
        "/transactions/",
        json={"account_destination_id": alice, "amount": 10.5, "transaction_type": "deposit"},
        headers={"Idempotency-Key": "k"},
    )

    assert response.status_code == 422


def test_deposit_with_a_source_is_rejected(
    make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice", 1000)
    bob = make_account("Bob")

    assert post_transaction("deposit", 10, source=alice, destination=bob).status_code == 422


def test_withdraw_with_a_destination_is_rejected(
    make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice", 1000)
    bob = make_account("Bob")

    assert post_transaction("withdraw", 10, source=alice, destination=bob).status_code == 422


def test_deposit_without_a_destination_is_rejected(post_transaction: PostTransaction) -> None:
    assert post_transaction("deposit", 10).status_code == 422


def test_transfer_without_a_destination_is_rejected(
    make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice", 1000)

    assert post_transaction("transfer", 10, source=alice).status_code == 422


def test_transfer_to_the_same_account_is_rejected(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)

    assert post_transaction("transfer", 10, source=alice, destination=alice).status_code == 422
    assert balance_of(alice) == 1000


# --------------------------------------------------------------------------- #
# Idempotency                                                                 #
# --------------------------------------------------------------------------- #
def test_replaying_a_key_returns_the_original_and_applies_it_once(
    client: TestClient,
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    balance_of: BalanceOf,
) -> None:
    alice = make_account("Alice", 1000)

    first = post_transaction("deposit", 200, destination=alice, key="same-key")
    second = post_transaction("deposit", 200, destination=alice, key="same-key")

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["transaction"] == second.json()["transaction"]  # same id, timestamp, ...
    assert balance_of(alice) == 1200
    assert len(history(client, alice)) == 2  # the seed deposit + this one, not three


def test_replaying_a_transfer_does_not_move_money_twice(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)
    bob = make_account("Bob", 0)

    for _ in range(3):
        post_transaction("transfer", 400, source=alice, destination=bob, key="t-1")

    assert balance_of(alice) == 600
    assert balance_of(bob) == 400


def test_reusing_a_key_with_a_different_body_is_a_409_and_changes_nothing(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 1000)
    post_transaction("deposit", 200, destination=alice, key="same-key")

    response = post_transaction("deposit", 999, destination=alice, key="same-key")

    assert response.status_code == 409
    assert balance_of(alice) == 1200


def test_same_body_with_different_keys_creates_separate_transactions(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 0)

    first = post_transaction("deposit", 50, destination=alice, key="k-1")
    second = post_transaction("deposit", 50, destination=alice, key="k-2")

    assert first.json()["transaction"]["id"] != second.json()["transaction"]["id"]
    assert balance_of(alice) == 100


def test_a_failed_attempt_does_not_burn_its_idempotency_key(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 100)
    assert post_transaction("withdraw", 500, source=alice, key="retry-me").status_code == 422
    post_transaction("deposit", 1000, destination=alice)

    retry = post_transaction("withdraw", 500, source=alice, key="retry-me")

    assert retry.status_code == 201
    assert balance_of(alice) == 600


def test_missing_idempotency_key_header_is_rejected(
    client: TestClient, make_account: MakeAccount
) -> None:
    alice = make_account("Alice", 1000)

    response = client.post(
        "/transactions/",
        json={"account_destination_id": alice, "amount": 200, "transaction_type": "deposit"},
    )

    assert response.status_code == 422


def test_empty_idempotency_key_header_is_rejected(
    client: TestClient, make_account: MakeAccount
) -> None:
    alice = make_account("Alice", 1000)

    response = client.post(
        "/transactions/",
        json={"account_destination_id": alice, "amount": 200, "transaction_type": "deposit"},
        headers={"Idempotency-Key": ""},
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Concurrency (through the HTTP layer)                                        #
# --------------------------------------------------------------------------- #
def test_concurrent_withdrawals_can_never_overdraw(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 100)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(
            pool.map(lambda _: post_transaction("withdraw", 10, source=alice), range(25))
        )

    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 10  # exactly enough money for ten
    assert statuses.count(422) == 15
    assert balance_of(alice) == 0


def test_concurrent_requests_with_one_key_apply_exactly_once(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 0)
    key = str(uuid.uuid4())

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(
            pool.map(
                lambda _: post_transaction("deposit", 50, destination=alice, key=key), range(10)
            )
        )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] * 9 + [201]
    assert balance_of(alice) == 50


def test_concurrent_transfers_conserve_money(
    make_account: MakeAccount, post_transaction: PostTransaction, balance_of: BalanceOf
) -> None:
    alice = make_account("Alice", 500)
    bob = make_account("Bob", 500)

    def move(i: int) -> int:
        if i % 2:
            return post_transaction("transfer", 7, source=alice, destination=bob).status_code
        return post_transaction("transfer", 7, source=bob, destination=alice).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(move, range(40)))

    assert balance_of(alice) + balance_of(bob) == 1000


# --------------------------------------------------------------------------- #
# Reading transactions                                                        #
# --------------------------------------------------------------------------- #
def test_get_transaction_by_id_returns_correct_fields(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice")
    created = post_transaction("deposit", 200, destination=alice).json()["transaction"]

    response = client.get(f"/transactions/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body == created
    assert body["account_source_id"] is None
    assert body["account_destination_id"] == alice
    assert body["amount"] == 200
    assert body["transaction_type"] == "deposit"
    assert body["timestamp"]


def test_get_nonexistent_transaction_returns_404(client: TestClient) -> None:
    assert client.get("/transactions/999").status_code == 404


def test_list_includes_transactions_where_the_account_is_source_or_destination(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice")
    bob = make_account("Bob")
    post_transaction("deposit", 500, destination=alice)  # alice: destination
    post_transaction("transfer", 300, source=alice, destination=bob)  # alice: source

    assert [t["transaction_type"] for t in history(client, alice)] == ["transfer", "deposit"]
    assert [t["transaction_type"] for t in history(client, bob)] == ["transfer"]


def test_list_for_an_account_with_no_transactions_is_empty(
    client: TestClient, make_account: MakeAccount
) -> None:
    alice = make_account("Alice")

    response = client.get(f"/transactions/?account_id={alice}")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_list_for_an_unknown_account_is_404(client: TestClient) -> None:
    assert client.get("/transactions/?account_id=999").status_code == 404


def test_list_without_account_id_returns_everything(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice")
    bob = make_account("Bob")
    post_transaction("deposit", 10, destination=alice)
    post_transaction("deposit", 10, destination=bob)

    assert len(history(client)) == 2


# --------------------------------------------------------------------------- #
# Pagination                                                                  #
# --------------------------------------------------------------------------- #
def _seed_deposits(
    make_account: MakeAccount, post_transaction: PostTransaction, count: int
) -> tuple[int, list[int]]:
    alice = make_account("Alice")
    ids = [
        post_transaction("deposit", 1, destination=alice).json()["transaction"]["id"]
        for _ in range(count)
    ]
    return alice, ids


def test_limit_caps_the_page_size(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice, _ = _seed_deposits(make_account, post_transaction, 3)

    body = client.get(f"/transactions/?account_id={alice}&limit=2").json()

    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None


def test_cursor_walks_every_transaction_exactly_once_newest_first(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice, ids = _seed_deposits(make_account, post_transaction, 5)

    seen: list[int] = []
    page_sizes: list[int] = []
    cursor: int | None = None
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        body = client.get(f"/transactions/?account_id={alice}&limit=2{suffix}").json()
        page_sizes.append(len(body["items"]))
        seen += [t["id"] for t in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert page_sizes == [2, 2, 1]
    assert seen == sorted(ids, reverse=True)  # newest first, no gaps, no repeats


def test_exact_multiple_of_page_size_ends_with_a_null_cursor(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice, _ = _seed_deposits(make_account, post_transaction, 4)

    first = client.get(f"/transactions/?account_id={alice}&limit=2").json()
    last = client.get(
        f"/transactions/?account_id={alice}&limit=2&cursor={first['next_cursor']}"
    ).json()

    assert len(last["items"]) == 2
    assert last["next_cursor"] is None


def test_cursor_past_the_oldest_transaction_returns_an_empty_page(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice, ids = _seed_deposits(make_account, post_transaction, 3)

    body = client.get(f"/transactions/?account_id={alice}&cursor={min(ids)}").json()

    assert body == {"items": [], "next_cursor": None}


def test_default_limit_is_ten(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice, _ = _seed_deposits(make_account, post_transaction, 12)

    body = client.get(f"/transactions/?account_id={alice}").json()

    assert len(body["items"]) == 10
    assert body["next_cursor"] is not None


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=-1", "limit=abc"])
def test_invalid_limit_is_rejected(
    client: TestClient, make_account: MakeAccount, query: str
) -> None:
    alice = make_account("Alice")

    assert client.get(f"/transactions/?account_id={alice}&{query}").status_code == 422


@pytest.mark.parametrize("cursor", ["abc", "0", "-5", "1.5"])
def test_invalid_cursor_is_rejected(
    client: TestClient, make_account: MakeAccount, cursor: str
) -> None:
    alice = make_account("Alice")

    assert client.get(f"/transactions/?account_id={alice}&cursor={cursor}").status_code == 422
