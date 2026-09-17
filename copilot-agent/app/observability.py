"""PHI-safe structured logging keyed by correlation id (ARCHITECTURE.md section 14).

Every log line is one JSON object with ``event`` and, where a request is in
scope, ``cid``. Fields are identifiers, timings, counts and outcome codes;
never clinical values, prompts, completions or raw patient identifiers. The
patient uuid is not logged at all by the agent - the correlation id is enough
to join with the module's audit row, which is where the patient is recorded.

Tracing to a self-hosted Langfuse is a later milestone; this module is the
in-process seam it will attach to (``span``).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

LOGGER_NAME = "copilot"

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: level, logger, event/message, and any extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = str(value) if isinstance(value, UUID) else value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "info") -> logging.Logger:
    """Attach a JSON handler to the ``copilot`` logger once. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(level.upper())
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(event: str, *, cid: UUID | str | None = None, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured line. ``fields`` must already be PHI-free."""
    extra: dict[str, Any] = {"event": event, **fields}
    if cid is not None:
        extra["cid"] = str(cid)
    get_logger().log(level, event, extra=extra)


@contextmanager
def span(name: str, *, cid: UUID | str | None = None, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a unit of work and log its outcome. The yielded dict collects PHI-free attributes."""
    attrs: dict[str, Any] = dict(fields)
    started = time.perf_counter()
    try:
        yield attrs
    except BaseException as exc:
        attrs["outcome"] = "error"
        attrs["error_type"] = type(exc).__name__
        raise
    else:
        attrs.setdefault("outcome", "ok")
    finally:
        attrs["duration_ms"] = int((time.perf_counter() - started) * 1000)
        log_event(f"span.{name}", cid=cid, **attrs)
        try:  # local metrics; never let accounting break the request
            from app.metrics import metrics as _metrics

            _metrics.observe(name, attrs["duration_ms"])
        except Exception:  # noqa: BLE001
            pass


__all__ = ["JsonFormatter", "LOGGER_NAME", "configure_logging", "get_logger", "log_event", "span"]
