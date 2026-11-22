"""HTTP layer. Thin: validate input, call ``ledger.db``, map errors to status codes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ledger import db
from ledger.db import TransactionType

UNPROCESSABLE = 422  # the status constant was renamed across Starlette versions


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


app = FastAPI(title="Transaction Ledger", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Models                                                                      #
# --------------------------------------------------------------------------- #
class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class AccountOut(BaseModel):
    id: int
    name: str
    balance: int


class AccountCreated(BaseModel):
    message: str
    account: AccountOut


class TransactionCreate(BaseModel):
    account_source_id: int | None = None
    account_destination_id: int | None = None
    amount: int = Field(gt=0, description="Positive integer, in minor units (e.g. cents)")
    transaction_type: TransactionType


class TransactionOut(BaseModel):
    id: int
    account_source_id: int | None
    account_destination_id: int | None
    amount: int
    transaction_type: TransactionType
    timestamp: str


class TransactionResult(BaseModel):
    message: str
    transaction: TransactionOut


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    next_cursor: int | None = Field(
        description="Pass as ?cursor= to fetch the next page. Null on the last page."
    )


def _transaction_out(transaction: db.Transaction) -> TransactionOut:
    return TransactionOut(
        id=transaction.id,
        account_source_id=transaction.account_source,
        account_destination_id=transaction.account_destination,
        amount=transaction.amount,
        transaction_type=transaction.transaction_type,
        timestamp=transaction.timestamp,
    )


# --------------------------------------------------------------------------- #
# Accounts                                                                    #
# --------------------------------------------------------------------------- #
@app.post("/accounts/", status_code=201, response_model=AccountCreated)
def create_account(body: AccountCreate) -> AccountCreated:
    account = db.create_account(body.name)
    return AccountCreated(
        message="Account created successfully",
        account=AccountOut(id=account.id, name=account.name, balance=account.balance),
    )


@app.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: int) -> AccountOut:
    account = db.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return AccountOut(id=account.id, name=account.name, balance=account.balance)


# --------------------------------------------------------------------------- #
# Transactions                                                                #
# --------------------------------------------------------------------------- #
@app.post("/transactions/", status_code=201, response_model=TransactionResult)
def create_transaction(
    body: TransactionCreate,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> TransactionResult:
    """Create a transaction.

    ``201`` when created. Replaying the same ``Idempotency-Key`` with the same
    body returns ``200`` and the original transaction without applying it again;
    reusing a key with a different body is a ``409``.
    """
    try:
        transaction, created = db.apply_transaction(
            idempotency_key=idempotency_key,
            transaction_type=body.transaction_type,
            amount=body.amount,
            source_id=body.account_source_id,
            destination_id=body.account_destination_id,
        )
    except db.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except db.IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (db.InsufficientFundsError, db.InvalidTransactionError) as exc:
        raise HTTPException(status_code=UNPROCESSABLE, detail=str(exc)) from exc

    if created:
        message = "Transaction processed successfully"
    else:
        response.status_code = 200
        message = "Transaction already processed with this idempotency key"
    return TransactionResult(message=message, transaction=_transaction_out(transaction))


@app.get("/transactions/{transaction_id}", response_model=TransactionOut)
def get_transaction(transaction_id: int) -> TransactionOut:
    transaction = db.get_transaction(transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _transaction_out(transaction)


@app.get("/transactions/", response_model=TransactionPage)
def list_transactions(
    account_id: Annotated[int | None, Query(description="Only this account's transactions")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    cursor: Annotated[
        int | None, Query(ge=1, description="`next_cursor` from the previous page")
    ] = None,
) -> TransactionPage:
    """Newest-first, cursor-paginated list of transactions."""
    if account_id is not None and db.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")
    page, next_cursor = db.list_transactions(account_id=account_id, limit=limit, cursor=cursor)
    return TransactionPage(items=[_transaction_out(t) for t in page], next_cursor=next_cursor)
