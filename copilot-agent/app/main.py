"""FastAPI entry point for the Clinical Co-Pilot agent service.

Routes (ARCHITECTURE.md sections 5, 10 and 14):

* ``POST /v1/bundles`` - module -> agent hand-off. HMAC-signed body; the
  ``ContextBundle`` is stored under a fresh ``bundle_id`` for a bounded TTL.
* ``GET /v1/briefings/{bundle_id}`` - panel -> agent. Gated by a signed,
  single-use, patient-bound ticket minted by the module. Streams the plan
  check as server-sent events; every event carries the correlation id and
  patient uuid, and a failure is an explicit ``degraded`` event.
* ``DELETE /v1/bundles/{bundle_id}`` - panel -> agent on unload or patient
  switch. Drops the bundle so any late request fails.
* ``POST /v1/briefings`` - synchronous evaluation path over an inline bundle
  (fixtures, evaluation harness, load tests). Not used by the panel.
* ``/health`` (liveness) and ``/ready`` (per-dependency readiness).

The model provider is injected: ``get_provider_factory`` builds the real
Anthropic provider lazily per briefing, so importing or starting the app
never requires ``ANTHROPIC_API_KEY``; tests override the dependency with a
fake provider.

Logging policy: request bodies and clinical content are never logged. Lines
are JSON with ``cid`` (see ``app.observability``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.contracts import (
    SCHEMA_VERSION,
    BriefingRequest,
    BriefingResponse,
    BundleAccepted,
    CommitmentEvent,
    CompleteEvent,
    ContextBundle,
    DegradedEvent,
    DegradedStage,
    DependencyStatus,
    ErrorDetail,
    HealthResponse,
    IntervalAnnotationEvent,
    TurnRequest,
    VerifiedTurn,
    ReadyResponse,
    StreamEnvelope,
)
from app.followup import ConversationTurn, run_turn
from app.observability import configure_logging, log_event, span
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import (
    MalformedModelOutputError,
    ModelProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ExpiredTicketError,
    InvalidSignatureError,
    InvalidTicketError,
    StaleSignatureError,
    TicketClaims,
    parse_bearer,
    verify_body_signature,
    verify_ticket,
)
from app.service import BriefingService
from app.settings import ModelSettings, ServiceSettings
from app.store import BundleStore, StoredBundle

CORRELATION_HEADER = "X-Correlation-Id"

settings = ServiceSettings()


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    application.state.store = BundleStore(ttl_seconds=settings.bundle_ttl_seconds)
    log_event("service.start", environment=settings.environment, bundle_ttl_seconds=settings.bundle_ttl_seconds)
    yield
    log_event("service.stop")


app = FastAPI(
    title="Clinical Co-Pilot Agent",
    version="0.2.0",
    summary="Plan-continuity briefings for a scheduled outpatient primary-care physician.",
    description=(
        "Read-only agent service. Accepts a minimum-necessary ContextBundle from the "
        "OpenEMR module, streams evidence-state matches with citations to the panel "
        f"under a patient-bound ticket. Synthetic data only. Contract schema version {SCHEMA_VERSION}."
    ),
    lifespan=_lifespan,
)

if settings.allowed_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.allowed_origin],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", CORRELATION_HEADER],
        expose_headers=[CORRELATION_HEADER],
        max_age=600,
    )


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def get_settings() -> ServiceSettings:
    return settings


def get_store(request: Request) -> BundleStore:
    return request.app.state.store


def get_provider_factory() -> Callable[[], ModelProvider]:
    """Return a factory that builds the configured provider on demand.

    Construction is deferred to the briefing call so a missing API key
    surfaces as an explicit error for that request, not as an import-time failure.
    """
    return lambda: AnthropicProvider(ModelSettings())


def get_briefing_service(
    provider_factory: Callable[[], ModelProvider] = Depends(get_provider_factory),
) -> BriefingService:
    return BriefingService(provider_factory)


# --------------------------------------------------------------------------- #
# Error mapping (fixed, non-clinical messages)
# --------------------------------------------------------------------------- #

_PROVIDER_ERROR_MAP: list[tuple[type[ProviderError], int, str, str]] = [
    (ProviderConfigurationError, status.HTTP_503_SERVICE_UNAVAILABLE, "provider_not_configured", "The model provider is not configured; the briefing could not be produced."),
    (ProviderAuthenticationError, status.HTTP_503_SERVICE_UNAVAILABLE, "provider_authentication_failed", "The model provider rejected the service credentials; the briefing could not be produced."),
    (ProviderTimeoutError, status.HTTP_504_GATEWAY_TIMEOUT, "provider_timeout", "The model provider did not respond in time; the briefing could not be produced."),
    (ProviderRateLimitError, status.HTTP_503_SERVICE_UNAVAILABLE, "provider_rate_limited", "The model provider is rate limiting requests; the briefing could not be produced."),
    (ProviderUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE, "provider_unavailable", "The model provider is unavailable; the briefing could not be produced."),
    (MalformedModelOutputError, status.HTTP_502_BAD_GATEWAY, "malformed_model_output", "The model returned output that failed validation; the briefing could not be produced."),
]


def _provider_error_code(exc: ProviderError) -> tuple[int, str, str]:
    for cls, http_status, code, message in _PROVIDER_ERROR_MAP:
        if isinstance(exc, cls):
            return http_status, code, message
    return status.HTTP_503_SERVICE_UNAVAILABLE, "provider_error", "The briefing could not be produced."  # pragma: no cover


def _provider_error_to_http(exc: ProviderError, correlation_id: UUID, patient_uuid: UUID) -> HTTPException:
    http_status, code, message = _provider_error_code(exc)
    detail = ErrorDetail(code=code, message=message, correlation_id=correlation_id, patient_uuid=patient_uuid)
    headers = {CORRELATION_HEADER: str(correlation_id)}
    if code == "provider_rate_limited":
        headers["Retry-After"] = "5"
    return HTTPException(status_code=http_status, detail=detail.model_dump(mode="json"), headers=headers)


def _error(http_status: int, code: str, message: str, *, correlation_id: UUID | None = None, headers: dict[str, str] | None = None) -> HTTPException:
    detail = ErrorDetail(code=code, message=message, correlation_id=correlation_id)
    return HTTPException(status_code=http_status, detail=detail.model_dump(mode="json", exclude_none=True), headers=headers)


def _validation_detail(exc: RequestValidationError | ValidationError) -> list[dict[str, Any]]:
    """Field locations and messages only - never the offending ``input`` value."""
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 body echoes the submitted value; for a clinical payload
    that would reflect note text back to the caller. Keep location and reason only."""
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": _validation_detail(exc)})


# --------------------------------------------------------------------------- #
# Trust-boundary dependencies
# --------------------------------------------------------------------------- #


def _require_secret(cfg: ServiceSettings) -> str:
    secret = cfg.ticket_secret_value()
    if secret is None:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "not_configured", "The shared ticket secret is not configured on this service.")
    return secret


async def require_signed_body(request: Request, cfg: ServiceSettings = Depends(get_settings)) -> bytes:
    """Verify the module's HMAC over the raw body before anything is parsed."""
    secret = _require_secret(cfg)
    body = await request.body()
    try:
        verify_body_signature(
            secret,
            body,
            request.headers.get(TIMESTAMP_HEADER),
            request.headers.get(SIGNATURE_HEADER),
            max_skew_seconds=cfg.signature_max_skew_seconds,
        )
    except StaleSignatureError as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, exc.code, "The bundle signature timestamp is outside the accepted window.") from None
    except InvalidSignatureError as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, exc.code, "The bundle signature is missing or does not verify.") from None
    return body


def _verify_ticket_or_401(secret: str, authorization: str | None, *, check_expiry: bool) -> TicketClaims:
    try:
        return verify_ticket(secret, parse_bearer(authorization), check_expiry=check_expiry)
    except ExpiredTicketError as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, exc.code, "The briefing ticket has expired; request a new one.") from None
    except InvalidTicketError as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, exc.code, "The briefing ticket is missing or invalid.") from None


def _bind_ticket_to_bundle(claims: TicketClaims, bundle_id: UUID, stored: StoredBundle | None) -> StoredBundle:
    """Fail closed on any disagreement between path, ticket and stored bundle (ARCH-002)."""
    if claims.bundle_id != bundle_id:
        raise _error(status.HTTP_403_FORBIDDEN, "ticket_bundle_mismatch", "The ticket was not issued for this bundle.", correlation_id=claims.cid)
    if stored is None:
        raise _error(status.HTTP_404_NOT_FOUND, "bundle_not_found", "The bundle has expired or was deleted; request a new briefing.", correlation_id=claims.cid)
    bundle = stored.bundle
    if bundle.patient_uuid != claims.puuid:
        raise _error(status.HTTP_403_FORBIDDEN, "ticket_patient_mismatch", "The ticket does not match the bundle's patient.", correlation_id=claims.cid)
    if bundle.correlation_id != claims.cid:
        raise _error(status.HTTP_403_FORBIDDEN, "ticket_correlation_mismatch", "The ticket does not match the bundle's correlation id.", correlation_id=claims.cid)
    if bundle.user_uuid is not None and str(bundle.user_uuid) != claims.sub:
        raise _error(status.HTTP_403_FORBIDDEN, "ticket_user_mismatch", "The ticket was not issued to the bundle's user.", correlation_id=claims.cid)
    return stored


async def require_briefing_ticket(
    bundle_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
    cfg: ServiceSettings = Depends(get_settings),
    store: BundleStore = Depends(get_store),
) -> tuple[TicketClaims, StoredBundle]:
    """Signature -> claims -> expiry -> binding -> single use, in that order."""
    secret = _require_secret(cfg)
    claims = _verify_ticket_or_401(secret, authorization, check_expiry=True)
    stored = _bind_ticket_to_bundle(claims, bundle_id, await store.get(bundle_id))
    if not await store.consume_jti(claims.jti, float(claims.exp)):
        raise _error(status.HTTP_409_CONFLICT, "ticket_reused", "The briefing ticket has already been used.", correlation_id=claims.cid)
    return claims, stored


async def require_turn_ticket(
    bundle_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
    cfg: ServiceSettings = Depends(get_settings),
    store: BundleStore = Depends(get_store),
) -> tuple[TicketClaims, StoredBundle]:
    """Same binding checks as the briefing stream; the jti is not consumed because a physician asks
    several questions within one ticket lifetime. Replay across bundles or patients still fails closed."""
    secret = _require_secret(cfg)
    claims = _verify_ticket_or_401(secret, authorization, check_expiry=True)
    stored = _bind_ticket_to_bundle(claims, bundle_id, await store.get(bundle_id))
    return claims, stored


async def require_delete_ticket(
    bundle_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
    cfg: ServiceSettings = Depends(get_settings),
) -> TicketClaims:
    """Deletion is allowed with an expired or already-used ticket - it only removes data -
    but the ticket must still verify and name this bundle."""
    secret = _require_secret(cfg)
    claims = _verify_ticket_or_401(secret, authorization, check_expiry=False)
    if claims.bundle_id != bundle_id:
        raise _error(status.HTTP_403_FORBIDDEN, "ticket_bundle_mismatch", "The ticket was not issued for this bundle.", correlation_id=claims.cid)
    return claims


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health() -> HealthResponse:
    """Liveness: the process is up."""
    return HealthResponse(status="ok")


@app.get("/ready", response_model=ReadyResponse, tags=["operations"], responses={503: {"model": ReadyResponse}})
async def ready(response: Response, cfg: ServiceSettings = Depends(get_settings), store: BundleStore = Depends(get_store)) -> ReadyResponse:
    """Readiness: every dependency the briefing path needs, with per-dependency status."""
    model = ModelSettings()
    deps = {
        "ticket_secret": DependencyStatus(status="ok") if cfg.has_ticket_secret() else DependencyStatus(status="not_configured", detail="COPILOT_TICKET_SECRET missing or too short"),
        "model_provider": DependencyStatus(status="ok", detail=f"{model.model_provider}:{model.model_id_extraction}") if model.has_api_key() else DependencyStatus(status="not_configured", detail="ANTHROPIC_API_KEY missing"),
        "bundle_store": DependencyStatus(status="ok", detail=f"{await store.count()} bundle(s) held"),
    }
    all_ok = all(d.status == "ok" for d in deps.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ready" if all_ok else "not_ready", dependencies=deps)


# --------------------------------------------------------------------------- #
# Hand-off: bundles
# --------------------------------------------------------------------------- #


@app.post(
    "/v1/bundles",
    response_model=BundleAccepted,
    status_code=status.HTTP_201_CREATED,
    tags=["bundles"],
    responses={401: {"model": ErrorDetail}, 422: {"description": "Bundle failed contract validation"}, 503: {"model": ErrorDetail}},
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ContextBundle"}}}, "required": True}},
)
async def store_bundle(
    response: Response,
    body: bytes = Depends(require_signed_body),
    store: BundleStore = Depends(get_store),
) -> BundleAccepted:
    """Accept a signed ``ContextBundle`` from the module and hold it for the briefing TTL."""
    try:
        bundle = ContextBundle.model_validate_json(body)
    except ValidationError as exc:
        # Same 422 shape and no-echo policy as body-parameter validation.
        raise RequestValidationError(exc.errors()) from None
    stored = await store.put(bundle)
    response.headers[CORRELATION_HEADER] = str(bundle.correlation_id)
    log_event("bundle.accepted", cid=bundle.correlation_id, bundle_id=stored.bundle_id, lab_results=len(bundle.lab_results), sources_unavailable=[s.value for s in bundle.data_quality.sources_unavailable])
    return BundleAccepted(
        bundle_id=stored.bundle_id,
        correlation_id=bundle.correlation_id,
        patient_uuid=bundle.patient_uuid,
        expires_at=stored.expires_at_datetime,
    )


@app.delete(
    "/v1/bundles/{bundle_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["bundles"],
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
async def delete_bundle(
    bundle_id: UUID,
    claims: TicketClaims = Depends(require_delete_ticket),
    store: BundleStore = Depends(get_store),
) -> Response:
    """Drop the bundle (patient switch, unload). Idempotent."""
    existed = await store.delete(bundle_id)
    log_event("bundle.deleted", cid=claims.cid, bundle_id=bundle_id, existed=existed)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={CORRELATION_HEADER: str(claims.cid)})


# --------------------------------------------------------------------------- #
# Briefing stream
# --------------------------------------------------------------------------- #


def _sse(event: str, payload: StreamEnvelope) -> str:
    return f"event: {event}\ndata: {payload.model_dump_json()}\n\n"


async def _briefing_events(
    stored: StoredBundle,
    service: BriefingService,
    timeout_seconds: float,
) -> AsyncIterator[str]:
    """Run the plan check and yield SSE frames. Every failure is a ``degraded`` frame."""
    bundle = stored.bundle
    ids = {"correlation_id": bundle.correlation_id, "patient_uuid": bundle.patient_uuid}
    cid = bundle.correlation_id
    with span("briefing", cid=cid, bundle_id=str(stored.bundle_id)) as attrs:
        # Stage 1: extraction (model). Every failure here is a degraded{extraction} frame.
        try:
            extraction, warnings = await asyncio.wait_for(service.extract(bundle), timeout=timeout_seconds)
        except TimeoutError:
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = "timeout"
            yield _sse("degraded", DegradedEvent(**ids, stage=DegradedStage.EXTRACTION, reason_code="timeout"))
            return
        except ProviderError as exc:
            _, code, _ = _provider_error_code(exc)
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = code
            yield _sse("degraded", DegradedEvent(**ids, stage=DegradedStage.EXTRACTION, reason_code=code))
            return
        except Exception:
            log_event("briefing.failed", cid=cid, level=logging.ERROR, stage="extraction", exc_info=True)
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = "internal_error"
            yield _sse("degraded", DegradedEvent(**ids, stage=DegradedStage.EXTRACTION, reason_code="internal_error"))
            return

        # Stage 2: deterministic matching and annotation. A failure here is degraded{matching}.
        try:
            result = service.assemble(bundle, extraction, warnings)
        except Exception:
            log_event("briefing.failed", cid=cid, level=logging.ERROR, stage="matching", exc_info=True)
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = "internal_error"
            yield _sse("degraded", DegradedEvent(**ids, stage=DegradedStage.MATCHING, reason_code="internal_error"))
            return

        stored.matches = result.matches
        for match in result.matches:
            yield _sse("commitment", CommitmentEvent(**ids, match=match))
        yield _sse("interval_annotation", IntervalAnnotationEvent(**ids, annotations=result.interval_annotations))
        attrs["commitments"] = len(result.matches)
        attrs["states"] = sorted({m.state.value for m in result.matches if m.state is not None})
        attrs["rejected"] = result.rejected_count
        attrs["interval_records"] = len(result.interval_annotations)
        attrs["unexplained"] = sum(1 for a in result.interval_annotations if a.explained_by is None)
        yield _sse("complete", CompleteEvent(**ids, commitments=len(result.matches), warnings=result.warnings, rejected_count=result.rejected_count))


@app.get(
    "/v1/briefings/{bundle_id}",
    tags=["briefings"],
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE: `commitment`*, `interval_annotation`, then `complete` | `degraded` (stage extraction|matching); every event carries correlation_id and patient_uuid."},
        401: {"model": ErrorDetail},
        403: {"model": ErrorDetail},
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
)
async def stream_briefing(
    ticket: tuple[TicketClaims, StoredBundle] = Depends(require_briefing_ticket),
    service: BriefingService = Depends(get_briefing_service),
    cfg: ServiceSettings = Depends(get_settings),
) -> StreamingResponse:
    """Stream the plan check for a stored bundle under a valid, single-use ticket."""
    claims, stored = ticket
    log_event("briefing.start", cid=claims.cid, bundle_id=str(stored.bundle_id))
    return StreamingResponse(
        _briefing_events(stored, service, cfg.briefing_timeout_seconds),
        media_type="text/event-stream",
        headers={
            CORRELATION_HEADER: str(claims.cid),
            "Cache-Control": "no-store, private",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Follow-up turns (UC-04)
# --------------------------------------------------------------------------- #


@app.post(
    "/v1/conversations/{bundle_id}/turns",
    response_model=VerifiedTurn,
    tags=["conversations"],
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
async def conversation_turn(
    request: TurnRequest,
    response: Response,
    ticket: tuple[TicketClaims, StoredBundle] = Depends(require_turn_ticket),
    service: BriefingService = Depends(get_briefing_service),
    cfg: ServiceSettings = Depends(get_settings),
) -> VerifiedTurn:
    """Answer one scoped question over the stored bundle. Statements are verified; failures are a degraded turn."""
    claims, stored = ticket
    bundle = stored.bundle
    ids = {"correlation_id": bundle.correlation_id, "patient_uuid": bundle.patient_uuid}
    response.headers[CORRELATION_HEADER] = str(claims.cid)
    matches = stored.matches if stored.matches is not None else []
    turn_index = len(stored.turns) + 1
    with span("turn", cid=claims.cid, bundle_id=str(stored.bundle_id), turn_index=turn_index) as attrs:
        try:
            outcome = await asyncio.wait_for(
                run_turn(service.provider(), bundle, matches, list(stored.turns), request.question),
                timeout=cfg.briefing_timeout_seconds,
            )
        except TimeoutError:
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = "timeout"
            return VerifiedTurn(**ids, turn_index=turn_index, degraded=DegradedEvent(**ids, stage=DegradedStage.TURN, reason_code="timeout"))
        except ProviderError as exc:
            _, code, _ = _provider_error_code(exc)
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = code
            return VerifiedTurn(**ids, turn_index=turn_index, degraded=DegradedEvent(**ids, stage=DegradedStage.TURN, reason_code=code))
        except Exception:
            log_event("turn.failed", cid=claims.cid, level=logging.ERROR, exc_info=True)
            attrs["outcome"] = "degraded"
            attrs["reason_code"] = "internal_error"
            return VerifiedTurn(**ids, turn_index=turn_index, degraded=DegradedEvent(**ids, stage=DegradedStage.TURN, reason_code="internal_error"))
        attrs["statements"] = len(outcome.statements)
        attrs["rejected"] = outcome.rejected_count
        attrs["rejection_codes"] = outcome.rejection_codes
        attrs["tool_calls"] = [t.tool for t in outcome.tool_calls]
        attrs["iterations"] = outcome.iterations
    stored.add_turn(ConversationTurn(question=request.question, statements=outcome.statements))
    return VerifiedTurn(**ids, turn_index=turn_index, statements=outcome.statements, rejected_count=outcome.rejected_count, tool_calls=outcome.tool_calls)


# --------------------------------------------------------------------------- #
# Synchronous evaluation path (fixtures, eval harness, load tests)
# --------------------------------------------------------------------------- #


@app.post(
    "/v1/briefings",
    response_model=BriefingResponse,
    status_code=status.HTTP_200_OK,
    tags=["briefings"],
    responses={
        502: {"description": "Model output failed validation", "model": ErrorDetail},
        503: {"description": "Model provider not configured, unauthenticated, rate limited or unavailable", "model": ErrorDetail},
        504: {"description": "Model provider timed out", "model": ErrorDetail},
    },
)
async def create_briefing(
    request: BriefingRequest,
    response: Response,
    service: BriefingService = Depends(get_briefing_service),
) -> BriefingResponse:
    """Produce a plan-continuity briefing for one inline bundle.

    Commitments are extracted from the prior plan (model proposes, grounding
    verifies spans), then matched to structured evidence deterministically.
    Provider failures return an explicit error rather than an empty briefing.
    """
    context = request.context
    response.headers[CORRELATION_HEADER] = str(context.correlation_id)
    try:
        with span("briefing.sync", cid=context.correlation_id):
            return await service.build_briefing(request)
    except ProviderError as exc:
        raise _provider_error_to_http(exc, context.correlation_id, context.patient_uuid) from None


__all__ = ["CORRELATION_HEADER", "app", "get_briefing_service", "get_provider_factory", "get_settings", "get_store"]
