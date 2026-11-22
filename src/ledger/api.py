"""HTTP layer. Thin: validate input, call ``ledger.db``, map errors to status codes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from ledger import db
from ledger.db import MAX_AMOUNT, MAX_INT64, TransactionType
from ledger.logging_config import configure_logging

logger = logging.getLogger("ledger.api")

UNPROCESSABLE = 422  # the status constant was renamed across Starlette versions
RETRY_AFTER_SECONDS = "1"

# Ids are positive and must fit SQLite's signed 64-bit integer; anything else is a 422
# at the edge instead of an OverflowError (500) deep inside the driver.
AccountId = Annotated[int, Field(ge=1, le=MAX_INT64)]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
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
    account_source_id: AccountId | None = None
    account_destination_id: AccountId | None = None
    amount: int = Field(
        gt=0,
        le=MAX_AMOUNT,
        description=f"Positive integer in minor units (e.g. cents), at most {MAX_AMOUNT}",
    )
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


def _busy() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="The ledger is busy, retry shortly",
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


# --------------------------------------------------------------------------- #
# Accounts                                                                    #
# --------------------------------------------------------------------------- #
# Collection routes are registered with and without the trailing slash so that neither
# form answers with a redirect (clients that don't follow redirects would see a 307).
@app.post("/accounts", status_code=201, response_model=AccountCreated, include_in_schema=False)
@app.post("/accounts/", status_code=201, response_model=AccountCreated)
def create_account(body: AccountCreate) -> AccountCreated:
    try:
        account = db.create_account(body.name)
    except db.DatabaseBusyError as exc:
        logger.warning("account_create_busy")
        raise _busy() from exc
    logger.info("account_created", extra={"account_id": account.id})
    return AccountCreated(
        message="Account created successfully",
        account=AccountOut(id=account.id, name=account.name, balance=account.balance),
    )


@app.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: Annotated[int, Path(ge=1, le=MAX_INT64)]) -> AccountOut:
    account = db.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return AccountOut(id=account.id, name=account.name, balance=account.balance)


# --------------------------------------------------------------------------- #
# Transactions                                                                #
# --------------------------------------------------------------------------- #
@app.post(
    "/transactions", status_code=201, response_model=TransactionResult, include_in_schema=False
)
@app.post("/transactions/", status_code=201, response_model=TransactionResult)
def create_transaction(
    body: TransactionCreate,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> TransactionResult:
    """Create a transaction.

    ``201`` when created. Replaying the same ``Idempotency-Key`` with the same
    body returns ``200`` and the original transaction without applying it again;
    reusing a key with a different body is a ``409``. ``503`` (with ``Retry-After``)
    means the ledger was busy and nothing was applied; retry with the same key.
    """
    request_fields = {
        "idempotency_key": idempotency_key,
        "tx_type": body.transaction_type,
        "amount": body.amount,
        "source_id": body.account_source_id,
        "destination_id": body.account_destination_id,
    }
    try:
        transaction, created = db.apply_transaction(
            idempotency_key=idempotency_key,
            transaction_type=body.transaction_type,
            amount=body.amount,
            source_id=body.account_source_id,
            destination_id=body.account_destination_id,
        )
    except db.DatabaseBusyError as exc:
        logger.warning("transaction_rejected", extra={**request_fields, "reason": "busy"})
        raise _busy() from exc
    except db.AccountNotFoundError as exc:
        logger.warning(
            "transaction_rejected", extra={**request_fields, "reason": "account_not_found"}
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except db.IdempotencyConflictError as exc:
        logger.warning(
            "transaction_rejected", extra={**request_fields, "reason": "idempotency_conflict"}
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except db.InsufficientFundsError as exc:
        logger.warning(
            "transaction_rejected", extra={**request_fields, "reason": "insufficient_funds"}
        )
        raise HTTPException(status_code=UNPROCESSABLE, detail=str(exc)) from exc
    except db.BalanceLimitExceededError as exc:
        logger.warning(
            "transaction_rejected", extra={**request_fields, "reason": "balance_limit_exceeded"}
        )
        raise HTTPException(status_code=UNPROCESSABLE, detail=str(exc)) from exc
    except db.InvalidTransactionError as exc:
        logger.warning("transaction_rejected", extra={**request_fields, "reason": "invalid"})
        raise HTTPException(status_code=UNPROCESSABLE, detail=str(exc)) from exc

    if created:
        logger.info("transaction_applied", extra={**request_fields, "tx_id": transaction.id})
        message = "Transaction processed successfully"
    else:
        logger.info("transaction_replayed", extra={**request_fields, "tx_id": transaction.id})
        response.status_code = 200
        message = "Transaction already processed with this idempotency key"
    return TransactionResult(message=message, transaction=_transaction_out(transaction))


@app.get("/transactions/{transaction_id}", response_model=TransactionOut)
def get_transaction(
    transaction_id: Annotated[int, Path(ge=1, le=MAX_INT64)],
) -> TransactionOut:
    transaction = db.get_transaction(transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _transaction_out(transaction)


@app.get("/transactions", response_model=TransactionPage, include_in_schema=False)
@app.get("/transactions/", response_model=TransactionPage)
def list_transactions(
    account_id: Annotated[
        int | None,
        Query(ge=1, le=MAX_INT64, description="Only this account's transactions"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    cursor: Annotated[
        int | None,
        Query(ge=1, le=MAX_INT64, description="`next_cursor` from the previous page"),
    ] = None,
) -> TransactionPage:
    """Newest-first, cursor-paginated list of transactions."""
    if account_id is not None and db.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")
    page, next_cursor = db.list_transactions(account_id=account_id, limit=limit, cursor=cursor)
    return TransactionPage(items=[_transaction_out(t) for t in page], next_cursor=next_cursor)
