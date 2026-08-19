"""
Structured logging (specification section 24).

One JSON object per line, with a request id bound for the lifetime of a request so
that every line emitted while handling it can be correlated. Values pass through
:mod:`app.core.sanitizer` on the way out, so a card number or an API key that ends
up in a log call is redacted rather than written to disk.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.sanitizer import sanitize

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

#: Attributes LogRecord always carries; anything else was added by the caller and
#: therefore belongs in the JSON payload.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
    "message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(sanitize(payload), default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # Uvicorn's access log duplicates our own request log line, in a different shape.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_logger(name: str) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger(name), {})
