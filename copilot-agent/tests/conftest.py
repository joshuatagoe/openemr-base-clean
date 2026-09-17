"""Shared test configuration.

* Async tests run on asyncio through the ``anyio`` pytest plugin.
* ``ANTHROPIC_API_KEY`` is removed from the environment for every test so no
  suite can construct the real provider by accident.
* ``client`` is a ``TestClient`` whose model provider is a scripted
  ``FakeProvider``; set the script with the ``provider_script`` fixture.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_provider_factory
from app.providers.base import ModelExtractionOutput
from tests.fakes import FakeProvider, hba1c, metformin, model_output

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"


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
def client(fake_provider: FakeProvider) -> Iterator[TestClient]:
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: fake_provider)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)


@pytest.fixture
def unconfigured_client() -> Iterator[TestClient]:
    """No override: the real provider factory runs with no API key configured."""
    app.dependency_overrides.pop(get_provider_factory, None)
    with TestClient(app) as test_client:
        yield test_client
