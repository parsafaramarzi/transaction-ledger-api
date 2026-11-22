"""Retry decorators for calls that are safe to repeat.

Both decorators are typed with ``ParamSpec`` so a decorated function keeps its exact
signature as far as mypy is concerned.

Only wrap a call that is safe to repeat. Here that means a request carrying an
``Idempotency-Key``: the key must be chosen *before* the first attempt and reused by every
retry, otherwise a retry after a lost response would apply the transaction twice.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

from ledger_client.models import LedgerBusyError

P = ParamSpec("P")
R = TypeVar("R")


def _delay(attempt: int, backoff: float, error: LedgerBusyError) -> float:
    """Exponential backoff, but never sooner than the server's ``Retry-After``."""
    return max(backoff * (2.0**attempt), error.retry_after or 0.0)


def _check(attempts: int) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")


def retry_on_busy(
    *,
    attempts: int = 3,
    backoff: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry a function when it raises :class:`LedgerBusyError`; re-raise after ``attempts``."""
    _check(attempts)

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except LedgerBusyError as exc:
                    if attempt == attempts - 1:
                        raise
                    sleep(_delay(attempt, backoff, exc))
            raise AssertionError("unreachable")  # the loop always returns or raises

        return wrapper

    return decorator


def async_retry_on_busy(
    *,
    attempts: int = 3,
    backoff: float = 0.1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """The ``async`` twin of :func:`retry_on_busy`."""
    _check(attempts)

    def decorator(func: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except LedgerBusyError as exc:
                    if attempt == attempts - 1:
                        raise
                    await sleep(_delay(attempt, backoff, exc))
            raise AssertionError("unreachable")

        return wrapper

    return decorator
