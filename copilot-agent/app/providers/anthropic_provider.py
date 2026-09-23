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
    ContentPart,
    DocumentPart,
    MalformedModelOutputError,
    ModelExtractionOutput,
    ModelExtractionResult,
    ModelTurnAnswer,
    ModelUsage,
    ParseResult,
    SchemaT,
    TextPart,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRejectedRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ToolCall,
    TurnStep,
)
from app.providers.prompt import EXTRACTION_SYSTEM_PROMPT, build_user_content
from app.providers.resilience import gate
from app.tools import strict_schema

SUBMIT_ANSWER_TOOL = "submit_answer"
from app.settings import ModelSettings


def _retry_after_seconds(exc: Exception) -> float | None:
    """The provider's Retry-After header in seconds, when present and sane (never the response body)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 60 else None


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

    @staticmethod
    def _to_blocks(content: list[ContentPart]) -> list[dict[str, Any]]:
        """Translate provider-neutral parts into Anthropic content blocks.

        This is the only place the vendor's content shape exists (dependency
        inversion, ADR design principles).
        """
        blocks: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, DocumentPart):
                block_type = "image" if part.media_type.startswith("image/") else "document"
                blocks.append({
                    "type": block_type,
                    "source": {"type": "base64", "media_type": part.media_type, "data": part.data_base64},
                })
            else:  # pragma: no cover - the union is closed
                raise ProviderConfigurationError("unsupported content part")
        return blocks

    def build_parse_request(
        self,
        *,
        system: str,
        content: list[ContentPart],
        schema: type[Any],
        max_tokens: int,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Keyword arguments for ``messages.parse``. Exposed for request-construction tests."""
        request: dict[str, Any] = {
            "model": self._settings.model_id_extraction,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": self._to_blocks(content)}],
            "output_format": schema,
        }
        if effort is not None:
            request["output_config"] = {"effort": effort}
        return request

    def build_request(self, plan_text: str) -> dict[str, Any]:
        """Week 1 commitment-extraction request. Retained for existing tests."""
        return self.build_parse_request(
            system=EXTRACTION_SYSTEM_PROMPT,
            content=[TextPart(text=build_user_content(plan_text))],
            schema=ModelExtractionOutput,
            max_tokens=self._settings.extraction_max_output_tokens,
            effort=self._settings.extraction_effort,
        )

    async def parse_structured(
        self,
        *,
        system: str,
        content: list[ContentPart],
        schema: type[SchemaT],
        max_tokens: int,
        effort: str | None = None,
    ) -> ParseResult[SchemaT]:
        request = self.build_parse_request(
            system=system, content=content, schema=schema, max_tokens=max_tokens, effort=effort
        )
        return await self._parse(request, schema)

    async def _parse(self, request: dict[str, Any], schema: type[SchemaT]) -> ParseResult[SchemaT]:
        started = time.perf_counter()
        try:
            async with gate.slot():
                response = await self._client.messages.parse(**request)
        except Exception as exc:  # noqa: BLE001 - mapped below
            self._raise_mapped(exc)
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.stop_reason != "end_turn":
            raise MalformedModelOutputError(f"model stopped before completing structured output ({response.stop_reason})")
        parsed = response.parsed_output
        if not isinstance(parsed, schema):
            raise MalformedModelOutputError("model output did not match the requested schema")

        usage = getattr(response, "usage", None)
        return ParseResult(
            output=parsed,
            usage=ModelUsage(
                provider=self.name,
                model=str(getattr(response, "model", self.model)),
                input_tokens=getattr(usage, "input_tokens", None),
                cached_input_tokens=getattr(usage, "cache_read_input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                latency_ms=latency_ms,
            ),
        )

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        """Week 1 convenience wrapper. Deliberately **not** a ``ModelProvider`` member."""
        result = await self._parse(self.build_request(plan_text), ModelExtractionOutput)
        return ModelExtractionResult(output=result.output, usage=result.usage)

    @staticmethod
    def _raise_mapped(exc: Exception) -> None:
        """Map SDK exceptions to the port's typed errors (fixed messages, nothing from the response)."""
        if isinstance(exc, ProviderError):
            raise exc
        try:
            raise exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderAuthenticationError("provider rejected the credentials") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError("provider request timed out") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitError("provider rate limit reached", retry_after=_retry_after_seconds(exc)) from exc
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise ProviderUnavailableError("provider unavailable") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailableError("provider unavailable") from exc
            raise ProviderRejectedRequestError("provider rejected the request") from exc
        except anthropic.APIResponseValidationError:
            raise MalformedModelOutputError("provider response did not match the expected shape") from None
        except (ValidationError, json.JSONDecodeError):
            # Pydantic errors carry the offending values; do not chain them.
            raise MalformedModelOutputError("model output did not match the extraction schema") from None

    # ------------------------------------------------------------------ #
    # Follow-up turn step: native tool use; the answer is itself a strict tool call
    # ------------------------------------------------------------------ #

    def build_turn_request(self, system: str, transcript: list[Any], tools: list[dict[str, Any]], *, force_answer: bool) -> dict[str, Any]:
        answer_schema = strict_schema(ModelTurnAnswer.model_json_schema())
        tool_params: list[dict[str, Any]] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"], "strict": True} for t in tools
        ] + [
            {
                "name": SUBMIT_ANSWER_TOOL,
                "description": "Submit the final answer: a list of short statements, each with its kind and the record_id values it cites.",
                "input_schema": answer_schema,
                "strict": True,
            }
        ]
        return {
            "model": self._settings.model_id_turn,
            "max_tokens": self._settings.turn_max_output_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": transcript,
            "tools": tool_params,
            "tool_choice": {"type": "tool", "name": SUBMIT_ANSWER_TOOL} if force_answer else {"type": "auto"},
            "output_config": {"effort": self._settings.turn_effort},
        }

    async def turn_step(self, system: str, transcript: list[Any], tools: list[dict[str, Any]], *, force_answer: bool) -> TurnStep:
        request = self.build_turn_request(system, transcript, tools, force_answer=force_answer)
        started = time.perf_counter()
        try:
            async with gate.slot():
                response = await self._client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - mapped below
            self._raise_mapped(exc)
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage_obj = getattr(response, "usage", None)
        usage = ModelUsage(
            provider=self.name,
            model=str(getattr(response, "model", self._settings.model_id_turn)),
            input_tokens=getattr(usage_obj, "input_tokens", None),
            cached_input_tokens=getattr(usage_obj, "cache_read_input_tokens", None),
            output_tokens=getattr(usage_obj, "output_tokens", None),
            latency_ms=latency_ms,
        )
        content = list(getattr(response, "content", []) or [])
        assistant_content = [self._block_to_param(b) for b in content]
        tool_calls: list[ToolCall] = []
        answer: ModelTurnAnswer | None = None
        for block in content:
            if getattr(block, "type", None) != "tool_use":
                continue
            raw_input = getattr(block, "input", {})
            if getattr(block, "name", "") == SUBMIT_ANSWER_TOOL:
                try:
                    answer = ModelTurnAnswer.model_validate(raw_input)
                except ValidationError:
                    raise MalformedModelOutputError("model answer did not match the turn schema") from None
            else:
                tool_calls.append(ToolCall(call_id=str(getattr(block, "id", "")), name=str(getattr(block, "name", "")), arguments=dict(raw_input) if isinstance(raw_input, dict) else {}))
        if answer is None and not tool_calls:
            if force_answer:
                raise MalformedModelOutputError("model did not submit an answer when required")
            answer = ModelTurnAnswer(statements=[])  # plain text with no tools and no answer: treated as nothing to say
        return TurnStep(tool_calls=tool_calls, answer=answer, usage=usage, assistant_content=assistant_content)

    async def ping(self) -> bool:
        """Models lookup for the configured extraction model: proves credentials and reachability without a completion."""
        try:
            await self._client.models.retrieve(self._settings.model_id_extraction)
            return True
        except Exception as exc:  # noqa: BLE001 - readiness must never raise
            try:
                self._raise_mapped(exc)
            except ProviderError:
                return False
            return False

    @staticmethod
    def _block_to_param(block: Any) -> dict[str, Any]:
        btype = getattr(block, "type", None)
        if btype == "text":
            return {"type": "text", "text": getattr(block, "text", "")}
        if btype == "tool_use":
            return {"type": "tool_use", "id": getattr(block, "id", ""), "name": getattr(block, "name", ""), "input": getattr(block, "input", {})}
        dump = getattr(block, "model_dump", None)
        return dump() if callable(dump) else {"type": "text", "text": ""}

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> dict[str, Any]:
        """User message carrying tool results (provider-shaped)."""
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content} for call_id, content in results]}


__all__ = ["AnthropicProvider"]
