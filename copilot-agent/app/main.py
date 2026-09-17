"""FastAPI entry point for the Clinical Co-Pilot agent service.

Vertical slice: ``POST /v1/briefings`` validates a ``ContextBundle``, runs
commitment extraction (model, then deterministic grounding) and deterministic
evidence matching, and returns a ``BriefingResponse``. No OpenEMR access, no
authentication, no readiness probe yet (ARCHITECTURE.md sections 5 and 17).

The model provider is injected: ``get_provider_factory`` builds the real
Anthropic provider lazily per briefing, so importing or starting the app
never requires ``ANTHROPIC_API_KEY``; tests override the dependency with a
fake provider.

Logging policy: request bodies and clinical content are never logged. The
default uvicorn access log records method, path and status only.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.contracts import (
    SCHEMA_VERSION,
    BriefingRequest,
    BriefingResponse,
    ErrorDetail,
    HealthResponse,
)
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
from app.service import BriefingService
from app.settings import ModelSettings

CORRELATION_HEADER = "X-Correlation-Id"


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (prefix ``COPILOT_``)."""

    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    app_name: str = "copilot-agent"
    environment: str = "development"
    log_level: str = "info"


settings = Settings()

app = FastAPI(
    title="Clinical Co-Pilot Agent",
    version="0.1.0",
    summary="Plan-continuity briefings for a scheduled outpatient primary-care physician.",
    description=(
        "Read-only agent service. Accepts a minimum-necessary ContextBundle, "
        "returns evidence-state matches with citations. Synthetic data only. "
        f"Contract schema version {SCHEMA_VERSION}."
    ),
)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def get_provider_factory() -> Callable[[], ModelProvider]:
    """Return a factory that builds the configured provider on demand.

    Construction is deferred to the briefing call so a missing API key
    surfaces as a 503 for that request, not as an import-time failure.
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


def _provider_error_to_http(exc: ProviderError, correlation_id: UUID, patient_uuid: UUID) -> HTTPException:
    for cls, http_status, code, message in _PROVIDER_ERROR_MAP:
        if isinstance(exc, cls):
            break
    else:  # pragma: no cover - every ProviderError subclass is listed above
        http_status, code, message = status.HTTP_503_SERVICE_UNAVAILABLE, "provider_error", "The briefing could not be produced."
    detail = ErrorDetail(code=code, message=message, correlation_id=correlation_id, patient_uuid=patient_uuid)
    headers = {CORRELATION_HEADER: str(correlation_id)}
    if code == "provider_rate_limited":
        headers["Retry-After"] = "5"
    return HTTPException(status_code=http_status, detail=detail.model_dump(mode="json"), headers=headers)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return field locations and messages only.

    FastAPI's default 422 body echoes the offending ``input`` value; for a
    clinical payload that would reflect note text back to the caller. Keep the
    error useful (where and why) without repeating what was sent.
    """
    detail = [{"loc": e.get("loc", ()), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": detail})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health() -> HealthResponse:
    """Liveness: the process is up. Readiness (dependency checks) is a later phase."""
    return HealthResponse(status="ok")


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
    """Produce a plan-continuity briefing for one patient bundle.

    Commitments are extracted from the prior plan (model proposes, grounding
    verifies spans), then matched to structured evidence deterministically.
    Provider failures return an explicit error rather than an empty briefing.
    """
    context = request.context
    response.headers[CORRELATION_HEADER] = str(context.correlation_id)
    try:
        return await service.build_briefing(request)
    except ProviderError as exc:
        raise _provider_error_to_http(exc, context.correlation_id, context.patient_uuid) from None
