"""Shared test configuration.

Async tests run on asyncio through the ``anyio`` pytest plugin (already a
transitive dependency of FastAPI); no trio backend is exercised.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
