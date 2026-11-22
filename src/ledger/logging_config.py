"""Structured (one JSON object per line) logging for the ``ledger`` loggers."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD_ATTRIBUTES = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line: time, level, logger, event + extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRIBUTES})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Send ``ledger.*`` logs to stderr as JSON. Safe to call more than once.

    The level comes from ``LEDGER_LOG_LEVEL`` (default ``INFO``).
    """
    logger = logging.getLogger("ledger")
    logger.setLevel(os.environ.get("LEDGER_LOG_LEVEL", "INFO").upper())
    if any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False  # don't print everything a second time via the root logger
