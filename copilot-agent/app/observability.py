"""PHI-safe structured logging and tracing keyed by correlation id (ARCHITECTURE.md section 14).

Every log line is one JSON object with ``event`` and, where a request is in
scope, ``cid``. Fields are identifiers, timings, counts and outcome codes;
never clinical values, prompts, completions or raw patient identifiers. The
patient uuid is not logged at all by the agent - the correlation id is enough
to join with the module's audit row, which is where the patient is recorded.

Tracing goes to a self-hosted Langfuse through the same seam (``span``,
``generation``, ``score``): one trace per correlation id (the trace id *is*
the cid), one observation per ``span``, one generation per model call, one
score per verification outcome. Everything sent passes through an allow-list
mask inside this process (``mask_for_tracing``), so plan text, statements,
drug or test names and patient identifiers can never leave as attributes:
they are not allow-listed keys. Model inputs and outputs are recorded only
when ``COPILOT_LANGFUSE_CAPTURE_IO`` is on, for synthetic-data evaluation runs.
Tracing is off entirely when no Langfuse keys are configured.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from langfuse import Langfuse

    from app.providers.base import ModelUsage

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


# --------------------------------------------------------------------------- #
# Langfuse tracing
# --------------------------------------------------------------------------- #

# The PHI control. Only these keys keep their value through the mask; every other
# value, and every free string outside them, is replaced. Clinical text has no key
# here by design, and neither does the patient uuid.
TRACE_ALLOWED_KEYS = frozenset(
    {
        "cid", "bundle_id", "turn_index", "outcome", "reason_code", "duration_ms", "stage",
        "commitments", "states", "rejected", "rejection_codes", "interval_records", "unexplained",
        "statements", "tool_calls", "iterations", "tool", "records", "truncated", "error",
        "model", "provider", "attempt", "input_tokens", "cached_input_tokens", "output_tokens",
        "latency_ms", "estimated_cost_usd", "error_type", "stop_reason", "effort",
    }
)
MASKED = "<masked>"

_langfuse: Langfuse | None = None
_capture_io = False


def mask_for_tracing(*, data: Any, **_: Any) -> Any:
    """Allow-list mask the Langfuse SDK applies to every input, output and metadata payload.

    Dict keys outside ``TRACE_ALLOWED_KEYS`` keep their name but lose their value; strings not
    under an allowed key are replaced; numbers, booleans and ``None`` pass. When
    ``COPILOT_LANGFUSE_CAPTURE_IO`` is on (synthetic data only) payloads pass through unchanged.
    """
    if _capture_io:
        return data
    return _mask_value(data, allowed=False)


def _mask_value(value: Any, *, allowed: bool) -> Any:
    if isinstance(value, dict):
        return {str(k): _mask_value(v, allowed=str(k) in TRACE_ALLOWED_KEYS) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(v, allowed=allowed) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (str, UUID)):
        return str(value) if allowed else MASKED
    return MASKED


def configure_tracing(
    *,
    enabled: bool,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    environment: str = "development",
    capture_io: bool = False,
    span_exporter: Any = None,
) -> bool:
    """Create the Langfuse client once, or leave tracing off. Returns whether tracing is on.

    ``span_exporter`` replaces the SDK's HTTP exporter (tests use an in-memory one)."""
    global _langfuse, _capture_io
    _capture_io = capture_io
    if not enabled:
        _langfuse = None
        return False
    import os

    from langfuse import Langfuse

    os.environ.setdefault("OTEL_SERVICE_NAME", "copilot-agent")  # resource attribute on every exported span
    extra: dict[str, Any] = {"span_exporter": span_exporter} if span_exporter is not None else {}
    _langfuse = Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url, environment=environment, mask=mask_for_tracing, **extra)
    log_event("tracing.configured", environment=environment, capture_io=capture_io)
    return True


def shutdown_tracing() -> None:
    global _langfuse
    if _langfuse is not None:
        try:
            _langfuse.flush()
            _langfuse.shutdown()
        except Exception:  # noqa: BLE001 - observability never fails the service
            pass
        _langfuse = None


def tracing_enabled() -> bool:
    return _langfuse is not None


def trace_id_for(cid: UUID | str) -> str:
    """The Langfuse trace id is the correlation id itself (32 hex, W3C-compatible)."""
    return UUID(str(cid)).hex


def _in_active_trace() -> bool:
    """Whether an OpenTelemetry span is current (checked directly: the SDK's own accessor logs an error when none is)."""
    from opentelemetry import trace

    return trace.get_current_span().get_span_context().is_valid


def _level_for(outcome: str) -> str:
    return "ERROR" if outcome in ("error", "degraded") else "DEFAULT"


def _start_observation(name: str, *, cid: UUID | str | None, as_type: str, **kwargs: Any) -> Any:
    """Open a Langfuse observation as the current one. Nests under an active trace; otherwise
    starts the trace whose id is ``cid``. Returns the context manager, or ``None`` when off."""
    if _langfuse is None:
        return None
    trace_context = None
    if not _in_active_trace():
        if cid is None:
            return None
        trace_context = {"trace_id": trace_id_for(cid)}
    return _langfuse.start_as_current_observation(name=name, as_type=as_type, trace_context=trace_context, **kwargs)


def _finish_observation(obs: Any, attrs: dict[str, Any], **update: Any) -> None:
    try:
        outcome = str(attrs.get("outcome", "ok"))
        status = attrs.get("reason_code") or attrs.get("error") or attrs.get("error_type")
        obs.update(metadata=attrs, level=_level_for(outcome), status_message=status, **update)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def span(name: str, *, cid: UUID | str | None = None, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a unit of work and log its outcome. The yielded dict collects PHI-free attributes.

    With tracing on, the same unit is a Langfuse observation carrying those attributes as
    metadata: the outermost ``span`` for a ``cid`` opens the trace, inner ones nest under it.
    """
    attrs: dict[str, Any] = dict(fields)
    started = time.perf_counter()
    obs_cm = _start_observation(name, cid=cid, as_type="span", metadata={"cid": str(cid)} if cid is not None else None)
    obs = obs_cm.__enter__() if obs_cm is not None else None
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
        if obs_cm is not None:
            _finish_observation(obs, attrs)
            obs_cm.__exit__(None, None, None)


@contextmanager
def generation(name: str, *, model: str | None = None, **fields: Any) -> Iterator[dict[str, Any]]:
    """Wrap one model call as a Langfuse generation with usage and cost.

    The caller sets ``attrs["usage"]`` (a ``ModelUsage``) on success and may set
    ``attrs["input"]``/``attrs["output"]``, which are forwarded only when capture is on.
    Errors are recorded by type, never by message. No-op when tracing is off.
    """
    attrs: dict[str, Any] = dict(fields)
    obs_cm = _start_observation(name, cid=None, as_type="generation", model=model)
    if obs_cm is None:
        yield attrs
        return
    obs = obs_cm.__enter__()
    try:
        yield attrs
    except BaseException as exc:
        attrs["outcome"] = "error"
        attrs["error_type"] = type(exc).__name__
        raise
    else:
        attrs.setdefault("outcome", "ok")
    finally:
        usage: ModelUsage | None = attrs.pop("usage", None)
        io_in, io_out = attrs.pop("input", None), attrs.pop("output", None)
        update: dict[str, Any] = {}
        if usage is not None:
            from app.metrics import estimate_cost_usd
            from app.settings import ModelSettings

            attrs.update(provider=usage.provider, model=usage.model, latency_ms=usage.latency_ms)
            tokens = {"input": usage.input_tokens, "cache_read_input_tokens": usage.cached_input_tokens, "output": usage.output_tokens}
            update["model"] = usage.model
            update["usage_details"] = {k: v for k, v in tokens.items() if v is not None}
            cost = estimate_cost_usd(usage.model, usage.input_tokens or 0, usage.cached_input_tokens or 0, usage.output_tokens or 0, ModelSettings().price_table())
            update["cost_details"] = {"total": round(cost, 6)}
        if _capture_io:
            update["input"], update["output"] = io_in, io_out
        _finish_observation(obs, attrs, **update)
        obs_cm.__exit__(None, None, None)


def score(name: str, value: float | int | bool, *, data_type: str = "NUMERIC") -> None:
    """Attach a score to the current trace (verification outcomes, degraded flag, state counts)."""
    if _langfuse is None or not _in_active_trace():
        return
    try:
        _langfuse.score_current_trace(name=name, value=float(value), data_type=data_type)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "JsonFormatter",
    "LOGGER_NAME",
    "MASKED",
    "TRACE_ALLOWED_KEYS",
    "configure_logging",
    "configure_tracing",
    "generation",
    "get_logger",
    "log_event",
    "mask_for_tracing",
    "score",
    "shutdown_tracing",
    "span",
    "trace_id_for",
    "tracing_enabled",
]
