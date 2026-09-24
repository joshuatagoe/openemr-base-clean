"""Record once, replay forever — the replay-harness pattern.

The problem this solves is real and was verified, not assumed: our golden cases
carry *scripted* model output, so the model never runs. Mutating
``EXTRACTION_SYSTEM_PROMPT`` to say "IGNORE ALL PRIOR RULES" left the gate
green. That makes the prompt decorative and `CR6`'s model-facing rubrics —
`factually_consistent`, `safe_refusal` — close to tautological, since they
score scripted output against expectations written from the same script.

The fix is not to call the model in CI. It is to call it **once**, freeze the
response to a committed fixture, and score that frozen snapshot for free
thereafter. The model is genuinely exercised, the artifact is deterministic,
and a grader with no API key can still run the gate.

What makes the prompt load-bearing: a recording is keyed by the digest of the
prompt that produced it, the model that produced it, and the input it saw.
Change any of the three and the key no longer matches, so the recording is
stale and the gate fails until it is re-recorded — and re-recording is exactly
the moment the quality change becomes visible.

Staleness is fatal rather than an automatic re-record. A recording that no
longer describes the code under test is not evidence, and silently refreshing
it would report a pass for a system nobody measured.

Recordings hold synthetic fixture data only. Never record against real PHI.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.providers.base import (
    ContentPart,
    DocumentPart,
    ModelProvider,
    ModelUsage,
    ParseResult,
    ProviderConfigurationError,
    SchemaT,
    TextPart,
)

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "recordings"


class StaleRecordingError(ProviderConfigurationError):
    """The prompt, model or input changed since this recording was made."""


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def content_key(content: list[ContentPart]) -> str:
    """Stable digest of the input, so a changed document invalidates too."""
    bits: list[str] = []
    for part in content:
        if isinstance(part, TextPart):
            bits.append(f"text:{part.text}")
        elif isinstance(part, DocumentPart):
            # Digest the bytes; never store them. A recording must not carry a
            # document payload around with it.
            bits.append(f"doc:{part.media_type}:{_digest(part.data_base64)}")
    return _digest(*bits)


@dataclass(frozen=True, slots=True)
class RecordingKey:
    case_id: str
    model: str
    system_digest: str
    content_digest: str

    @property
    def path(self) -> Path:
        return RECORDINGS_DIR / f"{self.case_id}.json"

    def matches(self, stored: dict[str, Any]) -> bool:
        return (
            stored.get("model") == self.model
            and stored.get("system_digest") == self.system_digest
            and stored.get("content_digest") == self.content_digest
        )

    def why_stale(self, stored: dict[str, Any]) -> str:
        if stored.get("system_digest") != self.system_digest:
            return "the prompt changed since this recording was made"
        if stored.get("model") != self.model:
            return f"recorded against {stored.get('model')!r}, now running {self.model!r}"
        return "the input document changed since this recording was made"


def _key(case_id: str, model: str, system: str, content: list[ContentPart]) -> RecordingKey:
    return RecordingKey(case_id, model, _digest(system), content_key(content))


class RecordingProvider:
    """Wraps a real provider and freezes every response to a committed fixture.

    Used only with ``--record``. Costs tokens; run deliberately, never in CI.
    """

    def __init__(self, inner: ModelProvider, case_id: str) -> None:
        self._inner = inner
        self._case_id = case_id
        self.name = f"recording({inner.name})"

    async def parse_structured(
        self,
        *,
        system: str,
        content: list[ContentPart],
        schema: type[SchemaT],
        max_tokens: int,
        effort: str | None = None,
    ) -> ParseResult[SchemaT]:
        result = await self._inner.parse_structured(
            system=system, content=content, schema=schema, max_tokens=max_tokens, effort=effort
        )
        key = _key(self._case_id, result.usage.model, system, content)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        key.path.write_text(
            json.dumps(
                {
                    "case_id": self._case_id,
                    "model": result.usage.model,
                    "system_digest": key.system_digest,
                    "content_digest": key.content_digest,
                    "schema": schema.__name__,
                    "output": result.output.model_dump(mode="json"),
                    "usage": result.usage.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return result

    async def turn_step(self, *a: Any, **k: Any) -> Any:
        return await self._inner.turn_step(*a, **k)

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> Any:
        raise NotImplementedError

    async def ping(self) -> bool:
        return await self._inner.ping()


class ReplayProvider:
    """Serves committed recordings: no network, no key, free, deterministic.

    This is what CI and a grader run.
    """

    name = "replay"

    def __init__(self, case_id: str, model: str) -> None:
        self._case_id = case_id
        self._model = model

    async def parse_structured(
        self,
        *,
        system: str,
        content: list[ContentPart],
        schema: type[SchemaT],
        max_tokens: int,
        effort: str | None = None,
    ) -> ParseResult[SchemaT]:
        key = _key(self._case_id, self._model, system, content)
        if not key.path.exists():
            raise StaleRecordingError(
                f"no recording for case {self._case_id!r}; run the eval with --record"
            )
        stored = json.loads(key.path.read_text(encoding="utf-8"))
        if not key.matches(stored):
            raise StaleRecordingError(
                f"recording for case {self._case_id!r} is stale: {key.why_stale(stored)}. "
                "Re-record with --record, and justify any change in pass rates."
            )
        if stored.get("schema") != schema.__name__:
            raise StaleRecordingError(
                f"recording for {self._case_id!r} holds {stored.get('schema')!r}, "
                f"expected {schema.__name__!r}"
            )
        return ParseResult(
            output=schema.model_validate(stored["output"]),
            usage=ModelUsage.model_validate(stored["usage"]),
        )

    async def turn_step(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError("replay covers structured parsing only")

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> Any:
        raise NotImplementedError

    async def ping(self) -> bool:
        return True


def recorded_case_ids() -> list[str]:
    if not RECORDINGS_DIR.exists():
        return []
    return sorted(p.stem for p in RECORDINGS_DIR.glob("*.json"))


__all__ = [
    "RECORDINGS_DIR",
    "RecordingKey",
    "RecordingProvider",
    "ReplayProvider",
    "StaleRecordingError",
    "content_key",
    "recorded_case_ids",
]
