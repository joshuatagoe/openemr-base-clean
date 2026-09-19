"""Shared test configuration.

* Async tests run on asyncio through the ``anyio`` pytest plugin.
* ``ANTHROPIC_API_KEY`` is removed and ``LANGFUSE_TRACING_ENABLED`` forced off for every test so no
  suite can construct the real provider or export traces by accident.
* ``client`` is a ``TestClient`` whose model provider is a scripted
  ``FakeProvider``; set the script with the ``provider_script`` fixture.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_provider_factory, get_settings
from app.providers.base import ModelExtractionOutput
from app.settings import ServiceSettings
from tests.fakes import FakeProvider, hba1c, metformin, model_output

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"

# Shared secret used by the configured test client. Long enough for the minimum-length check; not a real value.
TEST_TICKET_SECRET = "test-only-shared-secret-0123456789abcdef"


def configured_settings(**overrides: object) -> ServiceSettings:
    """Service settings with the ticket secret set and no ``.env`` influence."""
    values: dict[str, object] = {"ticket_secret": TEST_TICKET_SECRET, "briefing_timeout_seconds": 5.0}
    values.update(overrides)
    return ServiceSettings(_env_file=None, **values)  # type: ignore[arg-type]


def unconfigured_settings() -> ServiceSettings:
    return ServiceSettings(_env_file=None, ticket_secret=None)  # type: ignore[arg-type]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_api_key(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty value overrides any local .env entry (env vars take precedence
    # over env_file in pydantic-settings) and reads as "not configured".
    # Tests marked ``live`` (explicitly opted-in integration checks) keep the
    # environment as supplied.
    if "live" in request.node.keywords:
        return
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")  # never send test traces to the real Langfuse


@pytest.fixture
def fixture_payload() -> dict:
    """A fresh copy of the synthetic bundle per test so mutations never leak."""
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def provider_script() -> list[ModelExtractionOutput | Exception]:
    """Default script: the model proposes both fixture commitments. Tests may replace it."""
    return [model_output(metformin(), hba1c())]


@pytest.fixture
def fake_provider(provider_script: list[ModelExtractionOutput | Exception]) -> FakeProvider:
    return FakeProvider(*provider_script)


@pytest.fixture
def service_settings() -> ServiceSettings:
    """Settings for the configured client; tests may override this fixture."""
    return configured_settings()


@pytest.fixture
def client(fake_provider: FakeProvider, service_settings: ServiceSettings) -> Iterator[TestClient]:
    """Configured service (ticket secret set) with a scripted fake provider."""
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: fake_provider)
    app.dependency_overrides[get_settings] = lambda: service_settings
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def unconfigured_client() -> Iterator[TestClient]:
    """No provider override and no ticket secret: the real provider factory runs with no API key configured."""
    app.dependency_overrides.pop(get_provider_factory, None)
    app.dependency_overrides[get_settings] = unconfigured_settings
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_settings, None)
