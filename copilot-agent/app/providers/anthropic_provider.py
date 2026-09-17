"""AnthropicProvider: ModelProvider implementation over the official Anthropic SDK.

Structured output uses the SDK's native ``messages.parse(output_format=...)``,
which sends the Pydantic model's JSON schema as a constrained output format
and returns ``response.parsed_output`` already validated against
``ModelExtractionOutput``. No code fences, no prose scraping, no tool-call
workaround.

Retries are handled by the extractor (bounded, category-aware); the SDK's own
retry loop is disabled so total attempts stay explicit.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic
from pydantic import ValidationError

from app.providers.base import (
    MalformedModelOutputError,
    ModelExtractionOutput,
    ModelExtractionResult,
    ModelUsage,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.prompt import EXTRACTION_SYSTEM_PROMPT, build_user_content
from app.settings import ModelSettings


class AnthropicProvider:
    """Structured commitment extraction via ``claude`` models."""

    name = "anthropic"

    def __init__(self, settings: ModelSettings | None = None, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._settings = settings or ModelSettings()
        if client is None:
            if not self._settings.has_api_key():
                raise ProviderConfigurationError("ANTHROPIC_API_KEY is not configured")
            client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key.get_secret_value(),  # type: ignore[union-attr]
                timeout=self._settings.anthropic_timeout_seconds,
                max_retries=0,
            )
        self._client = client

    @property
    def model(self) -> str:
        return self._settings.model_id_extraction

    def build_request(self, plan_text: str) -> dict[str, Any]:
        """Keyword arguments for ``messages.parse``. Exposed for request-construction tests."""
        return {
            "model": self._settings.model_id_extraction,
            "max_tokens": self._settings.extraction_max_output_tokens,
            "system": [{"type": "text", "text": EXTRACTION_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": build_user_content(plan_text)}],
            "output_format": ModelExtractionOutput,
            "output_config": {"effort": self._settings.extraction_effort},
        }

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        request = self.build_request(plan_text)
        started = time.perf_counter()
        try:
            response = await self._client.messages.parse(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderAuthenticationError("provider rejected the credentials") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError("provider request timed out") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitError("provider rate limit reached") from exc
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise ProviderUnavailableError("provider unavailable") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailableError("provider unavailable") from exc
            raise ProviderConfigurationError("provider rejected the request") from exc
        except anthropic.APIResponseValidationError:
            raise MalformedModelOutputError("provider response did not match the expected shape") from None
        except (ValidationError, json.JSONDecodeError):
            # Pydantic errors carry the offending values; do not chain them.
            raise MalformedModelOutputError("model output did not match the extraction schema") from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.stop_reason != "end_turn":
            raise MalformedModelOutputError(f"model stopped before completing structured output ({response.stop_reason})")
        parsed = response.parsed_output
        if not isinstance(parsed, ModelExtractionOutput):
            raise MalformedModelOutputError("model output did not match the extraction schema")

        usage = getattr(response, "usage", None)
        return ModelExtractionResult(
            output=parsed,
            usage=ModelUsage(
                provider=self.name,
                model=str(getattr(response, "model", self.model)),
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                latency_ms=latency_ms,
            ),
        )


__all__ = ["AnthropicProvider"]
