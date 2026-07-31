"""Structured logging.

JSON by default because these logs are meant to be shipped and queried, not read
by a human tailing a terminal; `text` is available for local development. The
formatter promotes anything passed via ``extra=`` to a top-level field, which is
what makes lines like ``logger.info("accepted_order", extra={"orderId": ...})``
searchable rather than string-parsed later.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

#: Attributes present on every LogRecord. Anything not in here came from
#: ``extra=`` and is therefore application context worth emitting.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so an unexpected non-serialisable extra degrades to a string
        # instead of throwing inside the logging call and losing the line.
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable format that still shows the ``extra=`` context.

    The obvious `logging.Formatter("%(message)s")` silently discards everything
    passed via ``extra=``, which makes the text mode actively misleading during
    debugging: the event name is there but the ids and topics that explain it are
    not. Appending them keeps both formats equally informative.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }
        if not context:
            return base
        rendered = " ".join(
            f"{key}={value!r}" for key, value in sorted(context.items())
        )
        return f"{base} {rendered}"


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )

    root = logging.getLogger()
    # Idempotent: repeated calls (tests, reloads) must not duplicate every line.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These libraries are chatty at DEBUG and drown the application's own events.
    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("aiomqtt").setLevel(logging.INFO)
