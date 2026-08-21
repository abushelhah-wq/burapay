"""
Structured application logging (§6).

This is *application* logging -- startup, database errors, unhandled
exceptions. It is deliberately separate from ``api_call_logs``, which is
transaction data and lives in Postgres. Mixing the two would mean losing the
audit trail whenever log retention rotated.

Output is one JSON object per line on stdout, which is what Docker collects.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.timeutil import iso, utcnow

#: Attributes present on every LogRecord that are not extras worth emitting.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso(utcnow()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # default=str keeps a stray datetime or Decimal from turning a log
        # write into a second exception inside an exception handler.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn installs its own colourised handlers; replacing them keeps the
    # stream uniformly JSON so a collector does not need two parsers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
