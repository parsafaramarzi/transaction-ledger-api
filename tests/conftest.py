from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import BalanceOf, MakeAccount, PostTransaction

from ledger import db
from ledger.api import app


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at a throwaway database file with the schema created."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    return path


@pytest.fixture
def client(fresh_db: Path) -> TestClient:
    return TestClient(app)


@pytest.fixture
def post_transaction(client: TestClient) -> PostTransaction:
    """POST /transactions/ with sensible defaults; pass key= to reuse an idempotency key."""

    def _post(
        transaction_type: str,
        amount: int,
        *,
        source: int | None = None,
        destination: int | None = None,
        key: str | None = None,
    ) -> httpx.Response:
        response: httpx.Response = client.post(
            "/transactions/",
            json={
                "account_source_id": source,
                "account_destination_id": destination,
                "amount": amount,
                "transaction_type": transaction_type,
            },
            headers={"Idempotency-Key": key or str(uuid.uuid4())},
        )
        return response

    return _post


@pytest.fixture
def make_account(client: TestClient, post_transaction: PostTransaction) -> MakeAccount:
    """Create an account through the API, optionally funded by a seed deposit."""

    def _make(name: str = "Alice", balance: int = 0) -> int:
        account_id: int = client.post("/accounts/", json={"name": name}).json()["account"]["id"]
        if balance:
            assert post_transaction("deposit", balance, destination=account_id).status_code == 201
        return account_id

    return _make


@pytest.fixture
def balance_of(client: TestClient) -> BalanceOf:
    """Current balance of an account, read through the API."""

    def _balance(account_id: int) -> int:
        balance: int = client.get(f"/accounts/{account_id}").json()["balance"]
        return balance

    return _balance
