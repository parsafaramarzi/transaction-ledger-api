"""Type aliases shared by the fixtures in conftest.py and the tests that use them."""

from __future__ import annotations

from collections.abc import Callable

import httpx

PostTransaction = Callable[..., httpx.Response]
MakeAccount = Callable[..., int]
BalanceOf = Callable[[int], int]
