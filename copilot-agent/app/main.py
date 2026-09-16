"""FastAPI entry point for the Clinical Co-Pilot agent service.

Scaffold scope: contract validation and endpoint shapes only. No model call,
no evidence matching, no OpenEMR access, no authentication yet. See
ARCHITECTURE.md sections 5, 7 and 17 (Phase 0).

Logging policy: request bodies and clinical content are never logged. The
default uvicorn access log records method, path and status only.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.contracts import (
    SCHEMA_VERSION,
    BriefingRequest,
    BriefingResponse,
    HealthResponse,
)

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


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return field locations and messages only.

    FastAPI's default 422 body echoes the offending ``input`` value; for a
    clinical payload that would reflect note text back to the caller. Keep the
    error useful (where and why) without repeating what was sent.
    """
    detail = [{"loc": e.get("loc", ()), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": detail})


@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health() -> HealthResponse:
    """Liveness: the process is up. Readiness (dependency checks) is a later phase."""
    return HealthResponse(status="ok")


@app.post(
    "/v1/briefings",
    response_model=BriefingResponse,
    status_code=status.HTTP_200_OK,
    tags=["briefings"],
)
async def create_briefing(request: BriefingRequest, response: Response) -> BriefingResponse:
    """Validate a ContextBundle and return a briefing envelope.

    Extraction and deterministic evidence matching are not wired yet; the
    response carries the correlation and patient identifiers, an empty match
    list and an explicit warning so no caller mistakes this for a clinical
    conclusion.
    """
    context = request.context
    response.headers[CORRELATION_HEADER] = str(context.correlation_id)
    return BriefingResponse(
        correlation_id=context.correlation_id,
        patient_uuid=context.patient_uuid,
        matches=[],
        warnings=[
            "Extraction and evidence matching are not wired yet; no commitments were "
            "evaluated. Absence of matches is not a clinical finding."
        ],
    )
