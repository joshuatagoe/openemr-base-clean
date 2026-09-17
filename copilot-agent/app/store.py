"""In-memory bundle store with TTL (ARCHITECTURE.md section 8, "State and conversation boundaries").

State is ``{bundle_id -> ContextBundle + metadata}``, held only in process
memory, expired after a fixed TTL and deleted on patient switch. Nothing is
written to disk. The store also records used ticket ids (``jti``) so a ticket
cannot be replayed within its lifetime.

The store is process-local by design for the tracer bullet; a Redis-backed
implementation of the same interface is the scaling path (section 13).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.contracts import ContextBundle


@dataclass
class StoredBundle:
    bundle_id: UUID
    bundle: ContextBundle
    created_at: float
    expires_at: float

    @property
    def expires_at_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.expires_at, tz=timezone.utc)


@dataclass
class BundleStore:
    """TTL store keyed by ``bundle_id``. All methods are safe to call from one event loop."""

    ttl_seconds: float
    _bundles: dict[UUID, StoredBundle] = field(default_factory=dict)
    _used_jtis: dict[UUID, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _now(self) -> float:
        return time.time()

    def _sweep(self, now: float) -> None:
        for bundle_id in [b for b, s in self._bundles.items() if s.expires_at <= now]:
            del self._bundles[bundle_id]
        for jti in [j for j, exp in self._used_jtis.items() if exp <= now]:
            del self._used_jtis[jti]

    async def put(self, bundle: ContextBundle) -> StoredBundle:
        async with self._lock:
            now = self._now()
            self._sweep(now)
            stored = StoredBundle(bundle_id=uuid4(), bundle=bundle, created_at=now, expires_at=now + self.ttl_seconds)
            self._bundles[stored.bundle_id] = stored
            return stored

    async def get(self, bundle_id: UUID) -> StoredBundle | None:
        async with self._lock:
            now = self._now()
            self._sweep(now)
            return self._bundles.get(bundle_id)

    async def delete(self, bundle_id: UUID) -> bool:
        async with self._lock:
            return self._bundles.pop(bundle_id, None) is not None

    async def consume_jti(self, jti: UUID, expires_at: float) -> bool:
        """Mark ``jti`` used. Returns False if it was already used (replay)."""
        async with self._lock:
            now = self._now()
            self._sweep(now)
            if jti in self._used_jtis:
                return False
            self._used_jtis[jti] = max(expires_at, now + 1)
            return True

    async def count(self) -> int:
        async with self._lock:
            self._sweep(self._now())
            return len(self._bundles)


__all__ = ["BundleStore", "StoredBundle"]
