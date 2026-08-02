"""Typed Anthropic vision adapter for the ORBIT four-shot plan."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any, Literal, Protocol, Self

import anthropic
from pydantic import BaseModel, ConfigDict, Field, model_validator
from takegraph_domain.errors import (
    FeatureNotConfiguredError,
    InvalidSourceError,
    ProviderAuthError,
    ProviderQuotaError,
    ProviderUnavailableError,
)

SUPPORTED_REFERENCE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


class Shot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=1, le=4)
    title: str = Field(min_length=1, max_length=120)
    visual_direction: str = Field(min_length=1, max_length=800)
    camera: str = Field(min_length=1, max_length=300)
    motion: str = Field(min_length=1, max_length=300)
    duration_seconds: int = Field(ge=3, le=8)


class ShotPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["shot_plan.v1"] = "shot_plan.v1"
    shots: tuple[Shot, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def require_deterministic_indices(self) -> Self:
        if [shot.index for shot in self.shots] != [1, 2, 3, 4]:
            raise ValueError("shot plan indices must be exactly 1, 2, 3, 4 in order")
        return self


class PlanGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=128)
    brief: str = Field(min_length=1, max_length=5_000)
    product_reference_bytes: bytes = Field(min_length=1, max_length=20 * 1_048_576, repr=False)
    product_reference_mime: str
    product_reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_tokens: int = Field(default=1_600, ge=512, le=4_096)
    timeout_seconds: int = Field(default=120, ge=1, le=300)

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.product_reference_mime not in SUPPORTED_REFERENCE_TYPES:
            raise ValueError("product reference must be a supported Anthropic image type")
        digest = hashlib.sha256(self.product_reference_bytes).hexdigest()
        if digest != self.product_reference_sha256:
            raise ValueError("product reference bytes do not match their SHA-256")
        return self


class PlanGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_message_id: str
    model: str
    output: ShotPlan
    input_tokens: int
    output_tokens: int


class PlanGenerator(Protocol):
    async def generate(self, request: PlanGenerationRequest) -> PlanGenerationResult: ...


class AnthropicPlanGateway:
    def __init__(self, *, api_key: str, default_model: str) -> None:
        if not api_key or not default_model:
            raise FeatureNotConfiguredError("Anthropic shot planning is not configured.")
        self._api_key = api_key
        self._default_model = default_model

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AnthropicPlanGateway:
        missing = [name for name in ("ANTHROPIC_API_KEY", "EVALUATOR_MODEL") if not env.get(name)]
        if missing:
            raise FeatureNotConfiguredError(
                f"Anthropic shot planning is missing {', '.join(missing)}.",
                details={"missing": missing},
            )
        return cls(api_key=env["ANTHROPIC_API_KEY"], default_model=env["EVALUATOR_MODEL"])

    async def generate(self, request: PlanGenerationRequest) -> PlanGenerationResult:
        image_data = base64.b64encode(request.product_reference_bytes).decode("ascii")
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Creative brief:\n{request.brief}\n\n"
                    "Create exactly four sequential shots for a 16-second launch film. "
                    "Use the supplied product image as the identity reference. Keep the "
                    "bottle shape, label placement, colours, and proportions consistent."
                ),
            },
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": request.product_reference_mime,
                    "data": image_data,
                },
            },
        ]
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
                        "You are a production shot planner. Return only the requested typed "
                        "structure. Do not add product, health, legal, or performance claims. "
                        "Shot indices must be exactly 1 through 4 and each duration must "
                        "be 4 seconds."
                    ),
                    messages=[
                        {"role": "user", "content": content}  # type: ignore[typeddict-item]
                    ],
                    output_format=ShotPlan,
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
            raise ProviderUnavailableError("Anthropic shot planning was unavailable.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderUnavailableError(
                f"Anthropic shot planning failed with HTTP {exc.status_code}."
            ) from exc

        parsed = [
            block.parsed_output
            for block in message.content
            if block.type == "text" and block.parsed_output is not None
        ]
        if len(parsed) != 1 or not isinstance(parsed[0], ShotPlan):
            raise ProviderUnavailableError(
                "Anthropic completed without one schema-valid shot plan."
            )
        if message.stop_reason != "end_turn":
            raise ProviderUnavailableError(
                f"Anthropic shot planning ended with {message.stop_reason or 'unknown'} status."
            )
        if any(shot.duration_seconds != 4 for shot in parsed[0].shots):
            raise InvalidSourceError("Shot plan must allocate exactly four seconds per shot.")
        return PlanGenerationResult(
            provider_message_id=message.id,
            model=str(message.model),
            output=parsed[0],
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


__all__ = [
    "AnthropicPlanGateway",
    "PlanGenerationRequest",
    "PlanGenerationResult",
    "PlanGenerator",
    "Shot",
    "ShotPlan",
]
