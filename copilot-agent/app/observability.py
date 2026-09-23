"""PHI-safe structured logging and tracing keyed by correlation id (ARCHITECTURE.md section 14).

Every log line is one JSON object with ``event`` and, where a request is in
scope, ``cid``. Fields are identifiers, timings, counts and outcome codes;
never clinical values, prompts, completions or raw patient identifiers. The
patient uuid is not logged at all by the agent - the correlation id is enough
to join with the module's audit row, which is where the patient is recorded.

Tracing goes to a self-hosted Langfuse through the same seam (``span``,
``generation``, ``score``): one trace per correlation id (the trace id *is*
the cid), one observation per ``span``, one generation per model call, one
score per verification outcome.

Everything sent passes through an allow-list mask inside this process, applied
in the exporter by ``mask_otel_spans``: every attribute of every span this
client is about to send, whatever created it. That placement is the point.
The Langfuse SDK is OpenTelemetry-native, so spans written by third-party
instrumentation (LangGraph and anything else active in-process) are exported
too, and the SDK's legacy ``mask=`` hook never sees them - it only runs on
payloads the SDK itself sets. Plan text, statements, drug or test names and
patient identifiers therefore cannot leave as attributes: they are not
allow-listed keys. Model inputs and outputs are recorded only when
``COPILOT_LANGFUSE_CAPTURE_IO`` is on, for synthetic-data evaluation runs, and
never in a production environment.

Three limits of that mask shape the rest of this module (ADR-001 section 10.3):
it cannot alter span names, span ids or resource attributes, so those must be
opaque identifiers by rule (``is_opaque_identifier``); it inherits whatever
other OpenTelemetry instrumentation is active, so foreign instrumentation is
detected at startup and blocked; and a second exporter would receive its own
unmasked copy, so exactly one is ever registered.

Tracing is off entirely when no Langfuse keys are configured.
"""

from __future__ import annotations

import json
import logging
import re
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

# OpenTelemetry attribute keys that carry trace *structure*, not payload: observation type,
# level, status code, model name, token counts, cost. The Langfuse SDK sets them from codes
# and numbers it computes itself, so they survive the export mask. Every other attribute key,
# from any instrumentation, loses its value. Deliberately absent: the trace/observation input
# and output, session and user ids, tags, and anything a third party names.
TRACE_ALLOWED_OTEL_ATTRIBUTES = frozenset(
    {
        "langfuse.environment",
        "langfuse.internal.as_root",
        "langfuse.internal.is_app_root",
        "langfuse.observation.completion_start_time",
        "langfuse.observation.cost_details",
        "langfuse.observation.level",
        "langfuse.observation.model.name",
        "langfuse.observation.status_message",
        "langfuse.observation.type",
        "langfuse.observation.usage_details",
    }
)

# The Langfuse SDK flattens observation metadata into one attribute per key; the leaf name is
# the key ``span``/``generation`` was given, so ``TRACE_ALLOWED_KEYS`` applies to it directly.
_OTEL_METADATA_PREFIX = "langfuse.observation.metadata."

# Model input and output: JSON strings, masked to a JSON string so the shape still parses.
_OTEL_IO_ATTRIBUTES = frozenset(
    {"langfuse.observation.input", "langfuse.observation.output", "langfuse.trace.input", "langfuse.trace.output"}
)

# Instrumentation this service vouches for. The Langfuse SDK writes its own spans under
# ``langfuse-sdk``; anything else active in-process is exported too unless it is blocked.
VOUCHED_INSTRUMENTATION_SCOPES = frozenset({"langfuse-sdk"})

# Span names, tool names, node names and thread ids are opaque identifiers: lowercase, unspaced
# and bounded. ``mask_otel_spans`` cannot reach a span name, so the rule is enforced before the
# span is opened rather than at export.
_OPAQUE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,62}")
UNSAFE_NAME = "unsafe_span_name"

_langfuse: Langfuse | None = None
_capture_io = False
_blocked_scopes: frozenset[str] = frozenset()


def is_opaque_identifier(value: Any) -> bool:
    """Whether a value is safe where the export mask cannot reach (ADR-001 section 10.3).

    ``mask_otel_spans`` rewrites attributes only: span names, span ids and resource attributes
    are exported exactly as created. Node names, tool names and thread ids are therefore held to
    an identifier shape - lowercase ASCII, no whitespace, at most 63 characters - which no
    clinical phrase, name, date or measurement satisfies.
    """
    return isinstance(value, str) and _OPAQUE_IDENTIFIER.fullmatch(value) is not None


def _safe_name(name: str) -> str:
    """The span name to use: the given one, or a placeholder when it breaks the naming rule.

    The offending name is never logged - it is the thing suspected of carrying PHI.
    """
    if is_opaque_identifier(name):
        return name
    log_event("tracing.unsafe_span_name", level=logging.ERROR, length=len(name) if isinstance(name, str) else 0)
    return UNSAFE_NAME


def io_capture_enabled() -> bool:
    """Whether the ``COPILOT_LANGFUSE_CAPTURE_IO`` escape hatch is open.

    THIS IS THE ONE PATH ON WHICH MODEL INPUTS AND OUTPUTS LEAVE THIS PROCESS UNMASKED. It
    exists for synthetic-data evaluation runs. ``configure_tracing`` refuses it outright in a
    production environment and logs whenever it is open, so it cannot be turned on by accident.
    """
    return _capture_io


def mask_for_tracing(*, data: Any, **_: Any) -> Any:
    """The allow-list rule stated over one structured payload.

    Dict keys outside ``TRACE_ALLOWED_KEYS`` keep their name but lose their value; strings not
    under an allowed key are replaced; numbers, booleans and ``None`` pass. When
    ``COPILOT_LANGFUSE_CAPTURE_IO`` is on (synthetic data only) payloads pass through unchanged.

    This is no longer installed as the SDK's ``mask=`` hook - that hook only ran on payloads the
    SDK itself set, and never on third-party spans. It remains the payload-shaped statement of
    the rule that ``mask_otel_spans`` enforces attribute by attribute, over the same ``_mask_value``
    walk, so the two cannot drift.
    """
    if _capture_io:
        return data
    return _mask_value(data, allowed=False)


def mask_otel_spans(*, params: Any) -> Any:
    """Allow-list mask the Langfuse exporter applies to every span it is about to send.

    This is the PHI control for spans this module did not create. It runs at export, after
    Langfuse has chosen which spans to send, so it covers third-party OpenTelemetry
    instrumentation - LangGraph node and tool spans put graph state in their own attribute
    keys, which the SDK's legacy ``mask=`` hook never sees.

    An attribute keeps its value only if its key is allow-listed: trace structure
    (``TRACE_ALLOWED_OTEL_ATTRIBUTES``) or Langfuse metadata whose leaf name is in
    ``TRACE_ALLOWED_KEYS``. Everything else is replaced, whatever its type.

    Failure here is fail-closed by design: if this raises, Langfuse drops the whole export
    batch rather than sending it, which is the outcome to want if the mask is ever wrong.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches: dict[Any, OtelSpanPatch] = {}
    for identifier, span_data in params.spans.items():
        replacements = {}
        for key, value in span_data.attributes.items():
            masked = _mask_otel_attribute(str(key), value)
            if masked is not value:
                replacements[key] = masked
        if replacements:
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)
    return MaskOtelSpansResult(span_patches=patches)


def _mask_otel_attribute(key: str, value: Any) -> Any:
    if key in TRACE_ALLOWED_OTEL_ATTRIBUTES:
        return value
    if key in _OTEL_IO_ATTRIBUTES:
        return value if _capture_io else json.dumps(MASKED)
    if key.startswith(_OTEL_METADATA_PREFIX):
        return _mask_otel_metadata(value, allowed=key[len(_OTEL_METADATA_PREFIX) :] in TRACE_ALLOWED_KEYS)
    return MASKED


def _mask_otel_metadata(value: Any, *, allowed: bool) -> Any:
    """``TRACE_ALLOWED_KEYS`` semantics on one flattened metadata attribute.

    A list or dict metadata value arrives already serialised, so it is parsed back and masked
    per nested key - the same walk ``mask_for_tracing`` does before flattening.
    """
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return _mask_value(value, allowed=allowed)
        masked = _mask_value(parsed, allowed=allowed)
        return value if masked == parsed else json.dumps(masked, default=str)
    return _mask_value(value, allowed=allowed)


def foreign_instrumentation_scopes() -> list[str]:
    """OpenTelemetry instrumentation installed here that this service does not vouch for.

    The Langfuse SDK is OpenTelemetry-native: it exports whatever other instrumentation is
    active in this process, not only its own spans (ADR-001 section 10.3). This is checked at
    startup; whatever comes back is refused by ``should_export_span``, so it is dropped before
    export rather than trusted to the mask. An empty list - nothing installed - is the expected
    answer today, and the answer this service's dependencies give.
    """
    from importlib.metadata import entry_points

    scopes: set[str] = set()
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        # An instrumentor registers its tracer under its own name or under the conventional
        # ``opentelemetry.instrumentation.<name>``; block both spellings.
        scopes.update({entry_point.name, f"opentelemetry.instrumentation.{entry_point.name}"})
    return sorted(scopes - VOUCHED_INSTRUMENTATION_SCOPES)


def should_export_span(span: Any) -> bool:
    """Whether the Langfuse client exports one span at all - the gate ahead of the mask.

    Langfuse's own default already narrows export to its SDK, ``gen_ai`` spans and known LLM
    instrumentors. On top of that, anything ``foreign_instrumentation_scopes`` found at startup
    is refused outright: unvouched instrumentation is dropped, not masked and sent. (This is the
    supported replacement for the deprecated ``blocked_instrumentation_scopes`` argument.)
    """
    from langfuse.span_filter import is_default_export_span

    scope = getattr(span, "instrumentation_scope", None)
    if scope is not None and scope.name in _blocked_scopes:
        return False
    return is_default_export_span(span)


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

    Masking is installed as ``mask_otel_spans``: the export-stage hook, which covers spans from
    any instrumentation, rather than the legacy ``mask=``, which covers only the SDK's own
    payloads. Exactly one exporter is registered - a second one would receive its own unmasked
    copy of every span (ADR-001 section 10.3).

    ``span_exporter`` replaces the SDK's HTTP exporter (tests use an in-memory one); it is
    wrapped by the SDK's masking exporter, not added alongside it."""
    global _langfuse, _capture_io, _blocked_scopes
    _capture_io = _resolve_capture_io(capture_io=capture_io, environment=environment)
    if not enabled:
        _langfuse = None
        return False
    import os

    from langfuse import Langfuse

    os.environ.setdefault("OTEL_SERVICE_NAME", "copilot-agent")  # resource attribute on every exported span
    # Media handling runs in the exporter *before* ``mask_otel_spans`` and uploads any base64
    # data uri it finds to Langfuse storage. A scanned lab report is PHI by definition and this
    # service never sends media, so the uploader is switched off rather than masked around.
    os.environ["LANGFUSE_MEDIA_UPLOAD_ENABLED"] = "False"
    blocked = foreign_instrumentation_scopes()
    _blocked_scopes = frozenset(blocked)
    extra: dict[str, Any] = {"span_exporter": span_exporter} if span_exporter is not None else {}
    _langfuse = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        environment=environment,
        mask_otel_spans=mask_otel_spans,
        should_export_span=should_export_span,
        **extra,
    )
    log_event("tracing.configured", environment=environment, capture_io=_capture_io, blocked_scopes=blocked)
    return True


def _resolve_capture_io(*, capture_io: bool, environment: str) -> bool:
    """Whether to open the unmasked input/output escape hatch, loudly and never in production."""
    if not capture_io:
        return False
    if "prod" in environment.lower():
        log_event("tracing.io_capture_refused", level=logging.ERROR, environment=environment)
        return False
    log_event("tracing.io_capture_enabled", level=logging.WARNING, environment=environment)
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
    name = _safe_name(name)  # the export mask cannot rewrite a span name; the rule has to hold first
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
    name = _safe_name(name)  # the export mask cannot rewrite a span or score name
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
            attrs["estimated_cost_usd"] = round(cost, 6)
            score(f"{name}_cost_usd", round(cost, 6))  # averages give cost per briefing / per turn step (KEY_METRICS section 7)
        if _capture_io:
            update["input"], update["output"] = io_in, io_out
        _finish_observation(obs, attrs, **update)
        obs_cm.__exit__(None, None, None)


def score(name: str, value: float | int | bool | str, *, data_type: str = "NUMERIC") -> None:
    """Attach a score to the current trace (verification outcomes, degraded flag, state counts, outcome labels).

    Values are codes, counts or flags - never clinical content; CATEGORICAL values must be fixed labels."""
    if _langfuse is None or not _in_active_trace():
        return
    try:
        typed: float | str = str(value) if data_type == "CATEGORICAL" else float(value)
        _langfuse.score_current_trace(name=name, value=typed, data_type=data_type)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "JsonFormatter",
    "LOGGER_NAME",
    "MASKED",
    "TRACE_ALLOWED_KEYS",
    "TRACE_ALLOWED_OTEL_ATTRIBUTES",
    "UNSAFE_NAME",
    "VOUCHED_INSTRUMENTATION_SCOPES",
    "configure_logging",
    "configure_tracing",
    "foreign_instrumentation_scopes",
    "generation",
    "get_logger",
    "io_capture_enabled",
    "is_opaque_identifier",
    "log_event",
    "mask_for_tracing",
    "mask_otel_spans",
    "score",
    "should_export_span",
    "shutdown_tracing",
    "span",
    "trace_id_for",
    "tracing_enabled",
]
