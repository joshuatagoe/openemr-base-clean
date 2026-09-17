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
    """Environment variables: ANTHROPIC_API_KEY, MODEL_PROVIDER, MODEL_ID_EXTRACTION,
    ANTHROPIC_TIMEOUT_SECONDS, EXTRACTION_MAX_OUTPUT_TOKENS, EXTRACTION_EFFORT."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    anthropic_api_key: SecretStr | None = Field(default=None, description="Never logged; never a default.")
    model_provider: Literal["anthropic"] = "anthropic"
    model_id_extraction: str = Field(default="claude-opus-5", min_length=1)
    anthropic_timeout_seconds: float = Field(default=20.0, gt=0)
    extraction_max_output_tokens: int = Field(default=2048, ge=256, le=16000)
    extraction_effort: Literal["low", "medium", "high"] = "low"

    def has_api_key(self) -> bool:
        return self.anthropic_api_key is not None and bool(self.anthropic_api_key.get_secret_value().strip())
