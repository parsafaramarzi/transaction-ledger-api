"""The statement endpoint (join + window function), query plans, and reconciliation."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import BalanceOf, MakeAccount, PostTransaction

from ledger import db
from ledger.reconcile import main as reconcile_main


def statement(client: TestClient, account_id: int, query: str = "limit=100") -> dict[str, object]:
    response = client.get(f"/accounts/{account_id}/statement?{query}")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


@pytest.fixture
def story(make_account: MakeAccount, post_transaction: PostTransaction) -> tuple[int, int]:
    """Alice: +1000 deposit, -300 transfer to Bob, -100 withdrawal. Returns (alice, bob)."""
    alice = make_account("Alice")
    bob = make_account("Bob")
    post_transaction("deposit", 1000, destination=alice)
    post_transaction("transfer", 300, source=alice, destination=bob)
    post_transaction("withdraw", 100, source=alice)
    return alice, bob


# --------------------------------------------------------------------------- #
# The statement                                                               #
# --------------------------------------------------------------------------- #
def test_statement_shows_a_running_balance_signed_changes_and_counterparties(
    client: TestClient, story: tuple[int, int]
) -> None:
    alice, bob = story

    items = statement(client, alice)["items"]

    assert isinstance(items, list)
    rows = [
        (e["transaction_type"], e["change"], e["balance_after"], e["counterparty_name"])
        for e in items
    ]
    assert rows == [  # newest first
        ("withdraw", -100, 600, None),
        ("transfer", -300, 700, "Bob"),  # the join to accounts supplies the name
        ("deposit", 1000, 1000, None),
    ]
    assert items[1]["counterparty_id"] == bob


def test_the_other_side_of_a_transfer_sees_it_as_money_in(
    client: TestClient, story: tuple[int, int]
) -> None:
    alice, bob = story

    items = statement(client, bob)["items"]

    assert isinstance(items, list)
    assert [(e["change"], e["balance_after"], e["counterparty_name"]) for e in items] == [
        (300, 300, "Alice")
    ]


def test_closing_balance_equals_the_newest_lines_running_balance(
    client: TestClient, story: tuple[int, int], balance_of: BalanceOf
) -> None:
    alice, _ = story

    body = statement(client, alice)

    items = body["items"]
    assert isinstance(items, list)
    assert body["closing_balance"] == balance_of(alice) == 600
    assert items[0]["balance_after"] == body["closing_balance"]


def test_running_balance_stays_correct_across_pages(
    client: TestClient, make_account: MakeAccount, post_transaction: PostTransaction
) -> None:
    alice = make_account("Alice")
    amounts = [10, 20, 30, 40, 50, 60, 70]
    for amount in amounts:
        post_transaction("deposit", amount, destination=alice)

    seen: list[tuple[int, int]] = []
    cursor: object = None
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        body = statement(client, alice, f"limit=3{suffix}")
        items = body["items"]
        assert isinstance(items, list)
        seen += [(e["change"], e["balance_after"]) for e in items]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    running = [sum(amounts[: i + 1]) for i in range(len(amounts))]
    assert seen == list(zip(reversed(amounts), reversed(running), strict=True))


def test_statement_of_an_account_with_no_transactions_is_empty(
    client: TestClient, make_account: MakeAccount
) -> None:
    alice = make_account("Alice")

    assert statement(client, alice) == {
        "account_id": alice,
        "closing_balance": 0,
        "items": [],
        "next_cursor": None,
    }


def test_statement_for_an_unknown_account_is_404(client: TestClient) -> None:
    assert client.get("/accounts/999/statement").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/accounts/0/statement",
        f"/accounts/{10**30}/statement",
        "/accounts/1/statement?limit=0",
        "/accounts/1/statement?limit=101",
        "/accounts/1/statement?cursor=0",
        "/accounts/1/statement?cursor=abc",
    ],
)
def test_statement_rejects_invalid_input_with_422(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


def test_every_statement_ends_on_the_accounts_real_balance_after_concurrent_transfers(
    client: TestClient,
    make_account: MakeAccount,
    post_transaction: PostTransaction,
    balance_of: BalanceOf,
) -> None:
    alice = make_account("Alice", 500)
    bob = make_account("Bob", 500)

    def move(i: int) -> None:
        source, destination = (alice, bob) if i % 2 else (bob, alice)
        post_transaction("transfer", 7, source=source, destination=destination)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(move, range(40)))

    for account in (alice, bob):
        items = statement(client, account)["items"]
        assert isinstance(items, list)
        assert items[0]["balance_after"] == balance_of(account)
    assert db.reconcile() == []


# --------------------------------------------------------------------------- #
# Query plans                                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("fresh_db")
def test_finding_an_accounts_transactions_uses_the_indexes() -> None:
    plan = " | ".join(db.explain_account_entries(1))

    assert "idx_transactions_source" in plan
    assert "idx_transactions_destination" in plan
    assert "SCAN" not in plan  # no full table scan; every step is an index SEARCH


@pytest.mark.usefixtures("fresh_db")
def test_without_the_indexes_the_same_query_scans_the_whole_table() -> None:
    """Proves the test above can fail: drop the indexes and the plan changes."""
    with db.connection() as conn:
        conn.execute("DROP INDEX idx_transactions_source")
        conn.execute("DROP INDEX idx_transactions_destination")

    plan = " | ".join(db.explain_account_entries(1))

    assert "SCAN" in plan  # SQLite prints the table by its alias in the query ("SCAN t")
    assert "idx_transactions_" not in plan


# --------------------------------------------------------------------------- #
# Reconciliation                                                              #
# --------------------------------------------------------------------------- #
def test_a_healthy_ledger_reconciles(story: tuple[int, int]) -> None:
    assert db.reconcile() == []


@pytest.mark.usefixtures("fresh_db")
def test_an_empty_ledger_reconciles() -> None:
    assert db.reconcile() == []


def test_reconcile_detects_money_created_out_of_thin_air(story: tuple[int, int]) -> None:
    alice, _ = story
    with db.connection() as conn:
        conn.execute("UPDATE accounts SET balance = balance + 1 WHERE id = ?", (alice,))

    problems = db.reconcile()

    assert any("accounts hold" in p for p in problems)  # the global invariant
    assert any(f"account {alice} holds 601" in p for p in problems)  # and which account


def test_reconcile_detects_a_lost_transaction_record(story: tuple[int, int]) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM transactions WHERE transaction_type = 'withdraw'")

    problems = db.reconcile()

    assert problems  # balances now hold 100 more than the history explains


def test_reconcile_cli_exit_codes_in_process(
    story: tuple[int, int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert reconcile_main() == 0
    assert "OK" in capsys.readouterr().out

    with db.connection() as conn:
        conn.execute("UPDATE accounts SET balance = balance + 5")

    assert reconcile_main() == 1
    assert "MISMATCH" in capsys.readouterr().err


def test_reconcile_cli_returns_2_when_there_is_no_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "never_initialised.db"))

    assert reconcile_main() == 2
    assert "cannot reconcile" in capsys.readouterr().err


def test_reconcile_works_as_a_real_command_with_real_exit_codes(
    fresh_db: Path, story: tuple[int, int]
) -> None:
    """What a cron job or a shell `&&` chain would actually see."""
    env = {**os.environ, "LEDGER_DB_PATH": str(fresh_db)}
    run = [sys.executable, "-m", "ledger.reconcile"]

    ok = subprocess.run(run, env=env, capture_output=True, text=True, check=False)
    assert ok.returncode == 0
    assert ok.stdout.startswith("OK")

    with db.connection() as conn:
        conn.execute("UPDATE accounts SET balance = 0")

    bad = subprocess.run(run, env=env, capture_output=True, text=True, check=False)
    assert bad.returncode == 1
    assert "MISMATCH" in bad.stderr
