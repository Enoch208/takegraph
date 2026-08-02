"""Genblaze ElevenLabs adapter for durable narration assets."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from genblaze_core.exceptions import PipelineError, ProviderError
from genblaze_core.models.enums import Modality, PromptVisibility, ProviderErrorCode
from genblaze_core.pipeline import Pipeline
from genblaze_core.pipeline.result import PipelineResult
from genblaze_core.providers import DiscoveryResult
from genblaze_core.storage.base import KeyStrategy
from genblaze_core.storage.sink import ObjectStorageSink
from genblaze_elevenlabs import ElevenLabsTTSProvider
from genblaze_s3 import S3StorageBackend
from pydantic import BaseModel, ConfigDict, Field
from takegraph_domain.enums import ErrorClass
from takegraph_domain.errors import (
    FeatureNotConfiguredError,
    InvalidSourceError,
    ProviderAuthError,
    ProviderQuotaError,
    ProviderUnavailableError,
)
from takegraph_domain.generation import DurableGenerationAsset
from takegraph_infrastructure.b2 import B2Settings

from takegraph_worker.gmi_gateway import classify_provider_error, durable_assets

PipelineFactory = Callable[..., Any]
ProviderFactory = Callable[[], Any]
SinkFactory = Callable[["NarrationRequest"], Any]


@dataclass(frozen=True, slots=True)
class ElevenLabsSettings:
    api_key: str = field(repr=False)
    tts_model: str
    voice_id: str
    output_format: str = "mp3_44100_128"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ElevenLabsSettings:
        names = ("ELEVENLABS_API_KEY", "ELEVENLABS_TTS_MODEL", "ELEVENLABS_VOICE_ID")
        missing = [name for name in names if not env.get(name)]
        if missing:
            raise FeatureNotConfiguredError(
                f"ElevenLabs narration is not configured: missing {', '.join(missing)}.",
                details={"missing": missing},
            )
        return cls(
            api_key=env["ELEVENLABS_API_KEY"],
            tts_model=env["ELEVENLABS_TTS_MODEL"],
            voice_id=env["ELEVENLABS_VOICE_ID"],
            output_format=env.get("ELEVENLABS_OUTPUT_FORMAT") or "mp3_44100_128",
        )


class NarrationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_node_id: uuid.UUID
    attempt_id: uuid.UUID
    text: str = Field(min_length=1, max_length=5_000)
    model: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: int = Field(default=180, ge=1, le=300)


class NarrationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: DurableGenerationAsset


class NarrationGenerator(Protocol):
    async def generate(self, request: NarrationRequest) -> NarrationResult: ...


class ElevenLabsNarrationGateway:
    def __init__(
        self,
        settings: ElevenLabsSettings,
        b2_settings: B2Settings,
        *,
        pipeline_factory: PipelineFactory = Pipeline,
        provider_factory: ProviderFactory | None = None,
        sink_factory: SinkFactory | None = None,
    ) -> None:
        self._settings = settings
        self._b2_settings = b2_settings
        self._pipeline_factory = pipeline_factory
        self._provider_factory = provider_factory or self._make_provider
        self._sink_factory = sink_factory or self._make_sink

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ElevenLabsNarrationGateway:
        return cls(
            ElevenLabsSettings.from_env(env),
            B2Settings.from_env(dict(env)),
        )

    def _make_provider(self) -> ElevenLabsTTSProvider:
        return TakegraphElevenLabsTTSProvider(api_key=self._settings.api_key)

    def _make_sink(self, request: NarrationRequest) -> ObjectStorageSink:
        backend = S3StorageBackend.for_backblaze(
            self._b2_settings.bucket,
            region=self._b2_settings.region,
            key_id=self._b2_settings.key_id,
            app_key=self._b2_settings.app_key,
            preflight=False,
        )
        return ObjectStorageSink(
            backend,
            prefix=f"{self._b2_settings.prefix}/{request.organization_id}/genblaze",
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            strict_manifest_reads=True,
        )

    async def generate(self, request: NarrationRequest) -> NarrationResult:
        provider = self._provider_factory()
        pipeline = self._pipeline_factory(
            name="takegraph:audio.narration",
            tenant_id=str(request.organization_id),
            project_id=str(request.project_id),
            preflight=True,
        )
        pipeline.metadata(
            attempt_id=str(request.attempt_id),
            build_node_id=str(request.build_node_id),
            stable_key="audio.narration",
            idempotency_key=request.idempotency_key,
        )
        pipeline.step(
            provider,
            model=request.model,
            prompt=request.text,
            modality=Modality.AUDIO,
            prompt_visibility=PromptVisibility.PRIVATE,
            params={
                "voice_id": self._settings.voice_id,
                "output_format": self._settings.output_format,
                "with_timestamps": True,
            },
            metadata={"attempt_id": str(request.attempt_id), "stable_key": "audio.narration"},
        )
        try:
            result: PipelineResult = await pipeline.arun(
                sink=self._sink_factory(request),
                timeout=float(request.timeout_seconds),
                max_retries=0,
                raise_on_failure=True,
                _owns_sink=True,
            )
        except ProviderError as exc:
            raise _mapped_provider_error(exc.error_code) from exc
        except PipelineError as exc:
            code = _pipeline_error_code(exc)
            raise _mapped_provider_error(code) from exc
        outputs = durable_assets(result)
        if len(outputs) != 1:
            raise InvalidSourceError(
                f"ElevenLabs narration produced {len(outputs)} durable assets; expected one."
            )
        return NarrationResult(
            run_id=result.run.run_id,
            manifest_hash=result.manifest.canonical_hash,
            asset=outputs[0],
        )


def _pipeline_error_code(exc: PipelineError) -> ProviderErrorCode | None:
    if exc.result is None or exc.failed_step_index is None:
        return None
    steps = exc.result.run.steps
    if exc.failed_step_index < 0 or exc.failed_step_index >= len(steps):
        return None
    return steps[exc.failed_step_index].error_code


class TakegraphElevenLabsTTSProvider(ElevenLabsTTSProvider):
    """Compatibility shim for the connector's stale ElevenLabs 2.x method name.

    genblaze-elevenlabs 0.3.3 declares ``elevenlabs>=2,<3`` but calls
    ``models.get_all()``; every supported 2.x release exposes ``models.list()``.
    Keep the connector's cache and validation flow, replacing only that wire
    discovery method until an upstream release fixes it.
    """

    def _fetch_models(self) -> DiscoveryResult:
        try:
            # The connector intentionally types its lazy SDK client as Any.
            client: Any = self._get_client()  # type: ignore[no-untyped-call]
            models = client.models.list()
            slugs = frozenset(model.model_id for model in models if isinstance(model.model_id, str))
            return DiscoveryResult.ok(
                slugs,
                source_url="https://api.elevenlabs.io/v1/models",
            )
        except Exception as exc:
            return DiscoveryResult.failed(
                f"ElevenLabs models.list() failed: {exc}",
                source_url="https://api.elevenlabs.io/v1/models",
            )


def _mapped_provider_error(code: ProviderErrorCode | None) -> Exception:
    error_class = classify_provider_error(code)
    if error_class is ErrorClass.AUTH:
        return ProviderAuthError("ElevenLabs rejected the configured credential.")
    if error_class is ErrorClass.QUOTA:
        return ProviderQuotaError("ElevenLabs rate limit or quota was reached.")
    if error_class is ErrorClass.INPUT:
        return InvalidSourceError("ElevenLabs rejected the narration input.")
    return ProviderUnavailableError(
        f"ElevenLabs narration failed with {code.value if code is not None else 'unknown'} status."
    )


__all__ = [
    "ElevenLabsNarrationGateway",
    "ElevenLabsSettings",
    "NarrationGenerator",
    "NarrationRequest",
    "NarrationResult",
    "TakegraphElevenLabsTTSProvider",
]
