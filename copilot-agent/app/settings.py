"""Model-provider configuration (ARCHITECTURE.md section 14).

Read from the environment (or a local ``.env``, which is git-ignored). Nothing
here is required at import time; a missing API key only matters when the real
Anthropic provider is constructed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelSettings(BaseSettings):
    """Environment variables: ANTHROPIC_API_KEY, MODEL_PROVIDER, MODEL_ID_EXTRACTION, MODEL_ID_TURN,
    ANTHROPIC_TIMEOUT_SECONDS, EXTRACTION_MAX_OUTPUT_TOKENS, EXTRACTION_EFFORT, TURN_MAX_OUTPUT_TOKENS, TURN_EFFORT."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    anthropic_api_key: SecretStr | None = Field(default=None, description="Never logged; never a default.")
    model_provider: Literal["anthropic", "stub"] = "anthropic"
    model_id_extraction: str = Field(default="claude-opus-5", min_length=1)
    model_id_turn: str = Field(default="claude-opus-5", min_length=1)
    turn_max_output_tokens: int = Field(default=2048, ge=256, le=16000)
    turn_effort: Literal["low", "medium", "high"] = "low"
    anthropic_timeout_seconds: float = Field(default=20.0, gt=0)
    extraction_max_output_tokens: int = Field(default=2048, ge=256, le=16000)
    extraction_effort: Literal["low", "medium", "high"] = "low"

    price_input_per_mtok: float | None = Field(default=None, ge=0, description="Override: USD per million uncached input tokens for the configured model.")
    price_cached_per_mtok: float | None = Field(default=None, ge=0)
    price_output_per_mtok: float | None = Field(default=None, ge=0)

    def has_api_key(self) -> bool:
        return self.anthropic_api_key is not None and bool(self.anthropic_api_key.get_secret_value().strip())

    def is_configured(self) -> bool:
        return self.model_provider == "stub" or self.has_api_key()

    def price_table(self) -> dict[str, tuple[float, float, float]] | None:
        if self.price_input_per_mtok is None or self.price_output_per_mtok is None:
            return None
        cached = self.price_cached_per_mtok if self.price_cached_per_mtok is not None else self.price_input_per_mtok / 10
        return {self.model_id_extraction: (self.price_input_per_mtok, cached, self.price_output_per_mtok), self.model_id_turn: (self.price_input_per_mtok, cached, self.price_output_per_mtok)}


class ServiceSettings(BaseSettings):
    """Service configuration (prefix ``COPILOT_``; ARCHITECTURE.md sections 10 and 14).

    ``COPILOT_TICKET_SECRET`` is the shared secret with the OpenEMR module. It
    is required for ``POST /v1/bundles`` and the ticket-gated routes; when it
    is missing those routes answer 503 ``not_configured`` and ``/ready`` reports
    the gap. ``/health`` and the eval path never need it.
    """

    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "copilot-agent"
    environment: str = "development"
    log_level: str = "info"

    ticket_secret: SecretStr | None = Field(default=None, description="Shared HMAC/JWT key; never logged.")
    ticket_min_secret_length: int = Field(default=32, ge=16)
    bundle_ttl_seconds: int = Field(default=900, ge=60, le=3600, description="15 minutes by default.")
    signature_max_skew_seconds: int = Field(default=300, ge=10, le=3600)
    briefing_timeout_seconds: float = Field(default=10.0, gt=0, le=60, description="Hard timeout before a degraded event.")
    allowed_origin: str | None = Field(default=None, description="Browser origin of the OpenEMR panel (CORS). None disables CORS.")
    openemr_base_url: str | None = Field(default=None, description="For /ready: GET {url}/apis/default/fhir/metadata must answer. None skips the probe.")
    langfuse_host: str | None = Field(default=None, description="For /ready: GET {host}/api/public/health must answer. None skips the probe.")
    langfuse_capture_io: bool = Field(default=False, description="Record model inputs/outputs in traces. Synthetic-data evaluation runs only; never in production.")
    ready_probe_timeout_seconds: float = Field(default=5.0, gt=0, le=30, description="Per-probe HTTP timeout; the dev stack answers FHIR metadata in ~5.5 s with Xdebug on.")
    ready_cache_seconds: int = Field(default=60, ge=0, le=600)

    def ticket_secret_value(self) -> str | None:
        if self.ticket_secret is None:
            return None
        value = self.ticket_secret.get_secret_value().strip()
        if len(value) < self.ticket_min_secret_length:
            return None
        return value

    def has_ticket_secret(self) -> bool:
        return self.ticket_secret_value() is not None


class TracingSettings(BaseSettings):
    """Langfuse client credentials (no prefix: ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``,
    ``LANGFUSE_BASE_URL``, ``LANGFUSE_TRACING_ENABLED``). Tracing is off when either key is absent,
    so tests and local runs need no Langfuse. Keys are never logged."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None
    langfuse_tracing_enabled: bool = True

    def is_enabled(self) -> bool:
        return (
            self.langfuse_tracing_enabled
            and self.langfuse_public_key is not None
            and bool(self.langfuse_public_key.get_secret_value().strip())
            and self.langfuse_secret_key is not None
            and bool(self.langfuse_secret_key.get_secret_value().strip())
        )
