"""Typed direct Anthropic adapter for structured TAKEGRAPH copy nodes.

No Genblaze Anthropic connector is published for the pinned 0.3 line. This is
the PRD-approved direct-provider fallback: the official Anthropic SDK owns the
wire contract and Pydantic validates the structured output before persistence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import anthropic
from pydantic import BaseModel, ConfigDict, Field
from takegraph_domain.errors import (
    FeatureNotConfiguredError,
    ProviderAuthError,
    ProviderQuotaError,
    ProviderUnavailableError,
)


class CopyPack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    legal_line: str = Field(min_length=1, max_length=500)
    narration: str = Field(min_length=1, max_length=5_000)
    captions: tuple[str, ...] = Field(min_length=1, max_length=20)


class CopyGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    brief: str
    required_legal_line: str
    max_tokens: int = Field(default=900, ge=128, le=4_096)
    timeout_seconds: int = Field(default=120, ge=1, le=300)


class CopyGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_message_id: str
    model: str
    output: CopyPack
    input_tokens: int
    output_tokens: int


class CopyGenerator(Protocol):
    async def generate(self, request: CopyGenerationRequest) -> CopyGenerationResult: ...


class AnthropicCopyGateway:
    def __init__(self, *, api_key: str, default_model: str) -> None:
        if not api_key or not default_model:
            raise FeatureNotConfiguredError(
                "Anthropic structured-text generation is not configured."
            )
        self._api_key = api_key
        self._default_model = default_model

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AnthropicCopyGateway:
        missing = [name for name in ("ANTHROPIC_API_KEY", "EVALUATOR_MODEL") if not env.get(name)]
        if missing:
            raise FeatureNotConfiguredError(
                f"Anthropic structured-text generation is missing {', '.join(missing)}.",
                details={"missing": missing},
            )
        return cls(
            api_key=env["ANTHROPIC_API_KEY"],
            default_model=env["EVALUATOR_MODEL"],
        )

    async def generate(self, request: CopyGenerationRequest) -> CopyGenerationResult:
        model = request.model or self._default_model
        try:
            async with anthropic.AsyncAnthropic(
                api_key=self._api_key,
                timeout=float(request.timeout_seconds),
                max_retries=0,
            ) as client:
                message = await client.messages.parse(
                    model=model,
                    max_tokens=request.max_tokens,
                    temperature=0,
                    system=(
                        "You produce concise launch copy as structured data. Preserve the "
                        "required legal line exactly. Do not add product, health, or compliance "
                        "claims. The legal_line field must equal the supplied phrase verbatim."
                    ),
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Creative brief:\n{request.brief}\n\n"
                                f"Required legal line:\n{request.required_legal_line}\n\n"
                                "Return narration, the exact legal line, and 2-4 short captions."
                            ),
                        }
                    ],
                    output_format=CopyPack,
                )
        except anthropic.AuthenticationError as exc:
            raise ProviderAuthError("Anthropic rejected the configured credential.") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderQuotaError("Anthropic rate limit or quota was reached.") from exc
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ) as exc:
            raise ProviderUnavailableError("Anthropic generation was unavailable.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderUnavailableError(
                f"Anthropic generation failed with HTTP {exc.status_code}."
            ) from exc

        parsed = [
            block.parsed_output
            for block in message.content
            if block.type == "text" and block.parsed_output is not None
        ]
        if len(parsed) != 1 or not isinstance(parsed[0], CopyPack):
            raise ProviderUnavailableError(
                "Anthropic completed without one schema-valid copy-pack output."
            )
        if message.stop_reason != "end_turn":
            raise ProviderUnavailableError(
                f"Anthropic copy generation ended with {message.stop_reason or 'unknown'} status."
            )
        return CopyGenerationResult(
            provider_message_id=message.id,
            model=str(message.model),
            output=parsed[0],
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


__all__ = [
    "AnthropicCopyGateway",
    "CopyGenerationRequest",
    "CopyGenerationResult",
    "CopyGenerator",
    "CopyPack",
]
