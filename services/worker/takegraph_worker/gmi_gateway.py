"""Genblaze/GMI Cloud generation adapter (PRD §14).

One TAKEGRAPH graph node becomes one named Genblaze pipeline. Generated URLs
flow through ``ObjectStorageSink`` into private B2 before this adapter emits a
successful terminal event. A URL-only provider result therefore cannot satisfy
a build dependency (§14.5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, PromptVisibility, ProviderErrorCode
from genblaze_core.observability.events import (
    PipelineCompletedEvent,
    PipelineStartedEvent,
    StepCompletedEvent,
    StepProgressEvent,
    StepRetriedEvent,
    StreamEvent,
)
from genblaze_core.pipeline import Pipeline
from genblaze_core.pipeline.result import PipelineResult
from genblaze_core.storage.base import KeyStrategy
from genblaze_core.storage.sink import ObjectStorageSink
from genblaze_gmicloud import GMICloudImageProvider, GMICloudVideoProvider
from genblaze_s3 import S3StorageBackend
from pydantic import ValidationError
from takegraph_domain.enums import ErrorClass, NodeType
from takegraph_domain.errors import (
    AssetVerificationError,
    FeatureNotConfiguredError,
    ProviderAuthError,
    ProviderQuotaError,
    ProviderUnavailableError,
)
from takegraph_domain.generation import (
    AttemptRef,
    CancelResult,
    CancelState,
    DurableGenerationAsset,
    GenerationEvent,
    GenerationEventKind,
    GenerationRequest,
    ReconciliationResult,
    ReconciliationState,
)
from takegraph_infrastructure.b2 import B2Settings

GMI_QUEUE_BASE_URL = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"

PipelineFactory = Callable[..., Any]
ProviderFactory = Callable[[GenerationRequest], Any]
SinkFactory = Callable[[GenerationRequest], Any]


@dataclass(frozen=True, slots=True)
class GMICloudSettings:
    api_key: str = field(repr=False)
    image_model: str
    video_model: str
    video_fallback_model: str
    base_url: str = GMI_QUEUE_BASE_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GMICloudSettings:
        names = (
            "GMI_API_KEY",
            "GMI_IMAGE_MODEL",
            "GMI_VIDEO_MODEL",
            "GMI_VIDEO_FALLBACK_MODEL",
        )
        missing = [name for name in names if not env.get(name)]
        if missing:
            raise FeatureNotConfiguredError(
                f"GMI Cloud generation is not configured: missing {', '.join(missing)}.",
                details={"missing": missing},
            )
        base_url = env.get("GMI_BASE_URL") or GMI_QUEUE_BASE_URL
        if not base_url.startswith("https://") or base_url.rstrip("/").endswith("/v1"):
            raise FeatureNotConfiguredError(
                "GMI_BASE_URL must be the HTTPS request-queue endpoint, not the chat endpoint."
            )
        return cls(
            api_key=env["GMI_API_KEY"],
            image_model=env["GMI_IMAGE_MODEL"],
            video_model=env["GMI_VIDEO_MODEL"],
            video_fallback_model=env["GMI_VIDEO_FALLBACK_MODEL"],
            base_url=base_url.rstrip("/"),
        )


def classify_provider_error(code: ProviderErrorCode | None) -> ErrorClass:
    """Map the SDK enum once; retry policy never string-matches messages."""
    if code in (ProviderErrorCode.TIMEOUT, ProviderErrorCode.SERVER_ERROR):
        return ErrorClass.TRANSIENT
    if code is ProviderErrorCode.RATE_LIMIT:
        return ErrorClass.QUOTA
    if code is ProviderErrorCode.AUTH_FAILURE:
        return ErrorClass.AUTH
    if code is ProviderErrorCode.INVALID_INPUT:
        return ErrorClass.INPUT
    if code is ProviderErrorCode.MODEL_ERROR:
        return ErrorClass.MODEL
    if code is ProviderErrorCode.CONTENT_POLICY:
        return ErrorClass.POLICY
    return ErrorClass.INTERNAL


def normalized_gmi_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Translate TAKEGRAPH policy names into the installed connector surface.

    The SDK's video registry accepts ``duration``; the graph contract stores
    ``duration_seconds``. Translating here keeps provider wire names out of the
    domain and avoids inventing fields in Genblaze manifests (§14.6).
    """
    normalized = dict(parameters)
    if "duration_seconds" in normalized:
        if "duration" in normalized:
            raise ValueError("parameters cannot contain both duration and duration_seconds")
        normalized["duration"] = normalized.pop("duration_seconds")
    return normalized


def durable_assets(result: PipelineResult) -> tuple[DurableGenerationAsset, ...]:
    """Enforce the hash/size/manifest gate after the sink has completed."""
    manifest = result.manifest
    if not manifest.verify():
        raise AssetVerificationError(
            "Genblaze manifest verification failed after durable storage.",
            details={"run_id": result.run.run_id},
        )
    missing = manifest.output_asset_ids_missing_sha256()
    if missing:
        raise AssetVerificationError(
            "Generated output is missing a stored-byte SHA-256.",
            details={"run_id": result.run.run_id, "asset_ids": missing},
        )

    outputs: list[DurableGenerationAsset] = []
    for step in result.run.steps:
        for asset in step.assets:
            if not asset.sha256 or asset.size_bytes is None:
                raise AssetVerificationError(
                    "Generated output did not pass the durable storage gate.",
                    details={"run_id": result.run.run_id, "asset_id": asset.asset_id},
                )
            try:
                outputs.append(
                    DurableGenerationAsset(
                        asset_id=asset.asset_id,
                        durable_url=asset.url,
                        media_type=asset.media_type,
                        sha256=asset.sha256,
                        size_bytes=asset.size_bytes,
                    )
                )
            except ValidationError as exc:
                raise AssetVerificationError(
                    "Generated output contains non-durable asset metadata.",
                    details={"run_id": result.run.run_id, "asset_id": asset.asset_id},
                ) from exc
    if not outputs:
        raise AssetVerificationError(
            "Generation completed without a durable output asset.",
            details={"run_id": result.run.run_id},
        )
    return tuple(outputs)


class GMICloudGateway:
    """Production adapter plus injectable seams for contract tests."""

    def __init__(
        self,
        settings: GMICloudSettings,
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

    def _make_provider(self, request: GenerationRequest) -> Any:
        provider_type = (
            GMICloudImageProvider
            if request.node_type is NodeType.IMAGE_GENERATION
            else GMICloudVideoProvider
        )
        return provider_type(
            api_key=self._settings.api_key,
            base_url=self._settings.base_url,
            http_timeout=float(request.timeout_seconds),
        )

    def _make_sink(self, request: GenerationRequest) -> ObjectStorageSink:
        backend = S3StorageBackend.for_backblaze(
            self._b2_settings.bucket,
            region=self._b2_settings.region,
            key_id=self._b2_settings.key_id,
            app_key=self._b2_settings.app_key,
            preflight=False,
        )
        return ObjectStorageSink(
            backend,
            prefix=(f"{self._b2_settings.prefix}/{request.organization_id}/genblaze"),
            key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
            strict_manifest_reads=True,
        )

    def execute(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        return self._execute(request)

    async def _execute(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        if request.parent_run_id is not None:
            # Installed Genblaze 0.3.8 exposes parent linkage through
            # Pipeline.from_result(), not by accepting an arbitrary run id. A
            # caller must supply the persisted parent result in the retake slice;
            # silently dropping lineage would make the manifest dishonest.
            raise FeatureNotConfiguredError(
                "Parent-linked generation requires the persisted parent PipelineResult."
            )

        provider = self._provider_factory(request)
        sink = self._sink_factory(request)
        pipeline = self._pipeline_factory(
            name=f"takegraph:{request.stable_key}",
            tenant_id=str(request.organization_id),
            project_id=str(request.project_id),
            preflight=True,
        )
        pipeline.metadata(
            attempt_id=str(request.attempt_id),
            build_node_id=str(request.build_node_id),
            stable_key=request.stable_key,
            idempotency_key=request.idempotency_key,
        )
        modality = (
            Modality.IMAGE if request.node_type is NodeType.IMAGE_GENERATION else Modality.VIDEO
        )
        inputs = [
            Asset(
                asset_id=str(item.asset_id),
                url=item.url,
                media_type=item.media_type,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
            )
            for item in request.inputs
        ]
        pipeline.step(
            provider,
            model=request.model,
            prompt=request.prompt,
            modality=modality,
            fallback_models=list(request.fallback_models),
            external_inputs=inputs or None,
            prompt_visibility=PromptVisibility.PRIVATE,
            params=normalized_gmi_parameters(request.parameters),
            metadata={
                "attempt_id": str(request.attempt_id),
                "stable_key": request.stable_key,
            },
        )

        terminal_emitted = False
        seen_provider_request_ids: set[str] = set()
        try:
            async for event in pipeline.astream(
                sink=sink,
                timeout=float(request.timeout_seconds),
                max_retries=request.max_retries,
                raise_on_failure=True,
                _owns_sink=True,
            ):
                async for mapped in self._map_event(
                    request, event, seen_provider_request_ids=seen_provider_request_ids
                ):
                    if mapped.kind in (GenerationEventKind.COMPLETED, GenerationEventKind.FAILED):
                        terminal_emitted = True
                    yield mapped
        except ProviderError as exc:
            if not terminal_emitted:
                yield self._failure_event(request, exc.error_code)
        except Exception:
            if not terminal_emitted:
                yield self._failure_event(request, None)
            raise
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    async def _map_event(
        self,
        request: GenerationRequest,
        event: StreamEvent,
        *,
        seen_provider_request_ids: set[str],
    ) -> AsyncIterator[GenerationEvent]:
        base = {
            "attempt_id": request.attempt_id,
            "provider": request.provider,
            "model": request.model,
        }
        if isinstance(event, PipelineStartedEvent):
            yield GenerationEvent(kind=GenerationEventKind.RUN_STARTED, run_id=event.run_id, **base)
        elif isinstance(event, StepProgressEvent):
            first_request_event = bool(
                event.request_id and event.request_id not in seen_provider_request_ids
            )
            if event.request_id:
                seen_provider_request_ids.add(event.request_id)
            kind = (
                GenerationEventKind.PROVIDER_SUBMITTED
                if first_request_event
                else GenerationEventKind.PROVIDER_PROGRESS
            )
            yield GenerationEvent(
                kind=kind,
                run_id=event.run_id,
                provider_request_id=event.request_id,
                progress=event.progress_pct,
                **base,
            )
        elif isinstance(event, StepRetriedEvent):
            yield GenerationEvent(
                kind=GenerationEventKind.PROVIDER_RETRY,
                run_id=event.run_id,
                retry_attempt=event.attempt,
                error_code=event.error_code,
                **base,
            )
        elif isinstance(event, StepCompletedEvent):
            yield GenerationEvent(
                kind=GenerationEventKind.PROVIDER_COMPLETED,
                run_id=event.run_id,
                provider_request_id=event.request_id,
                **base,
            )
        elif isinstance(event, PipelineCompletedEvent) and event.result is not None:
            for asset in durable_assets(event.result):
                yield GenerationEvent(
                    kind=GenerationEventKind.STORED,
                    run_id=event.run_id,
                    asset=asset,
                    **base,
                )
            yield GenerationEvent(
                kind=GenerationEventKind.COMPLETED,
                run_id=event.run_id,
                manifest_hash=event.result.manifest.canonical_hash,
                **base,
            )

    @staticmethod
    def _failure_event(
        request: GenerationRequest, code: ProviderErrorCode | None
    ) -> GenerationEvent:
        return GenerationEvent(
            kind=GenerationEventKind.FAILED,
            attempt_id=request.attempt_id,
            provider=request.provider,
            model=request.model,
            error_class=classify_provider_error(code),
            error_code=code.value if code is not None else "unknown",
            message="Generation failed; inspect the attempt record with its correlation ID.",
        )

    async def reconcile(self, attempt: AttemptRef) -> ReconciliationResult:
        response = await self._request("GET", f"/requests/{attempt.provider_request_id}")
        if response.status_code == 404:
            return ReconciliationResult(
                state=ReconciliationState.NOT_FOUND,
                provider_request_id=attempt.provider_request_id,
            )
        self._raise_for_provider_status(response)
        status = str(response.json().get("status", "")).lower()
        state = {
            "queued": ReconciliationState.PENDING,
            "dispatched": ReconciliationState.RUNNING,
            "processing": ReconciliationState.RUNNING,
            "success": ReconciliationState.PROVIDER_SUCCEEDED,
            "failed": ReconciliationState.FAILED,
            "cancelled": ReconciliationState.CANCELLED,
        }.get(status, ReconciliationState.RUNNING)
        return ReconciliationResult(
            state=state,
            provider_request_id=attempt.provider_request_id,
        )

    async def cancel(self, attempt: AttemptRef) -> CancelResult:
        response = await self._request("DELETE", f"/requests/{attempt.provider_request_id}")
        if response.status_code == 404:
            return CancelResult(
                state=CancelState.NOT_FOUND,
                provider_request_id=attempt.provider_request_id,
            )
        if response.status_code in (409, 410):
            return CancelResult(
                state=CancelState.ALREADY_TERMINAL,
                provider_request_id=attempt.provider_request_id,
            )
        self._raise_for_provider_status(response)
        return CancelResult(
            state=CancelState.CANCELLED,
            provider_request_id=attempt.provider_request_id,
        )

    async def _request(self, method: str, path: str) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self._settings.base_url,
            headers={"Authorization": f"Bearer {self._settings.api_key}"},
            timeout=20.0,
        ) as client:
            try:
                return await client.request(method, path)
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(
                    "GMI Cloud is unreachable; reconciliation did not change attempt state."
                ) from exc

    @staticmethod
    def _raise_for_provider_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise ProviderAuthError("GMI Cloud rejected the configured inference credential.")
        if response.status_code == 429:
            raise ProviderQuotaError("GMI Cloud rate limit or quota was reached.")
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"GMI Cloud request failed with HTTP {response.status_code}."
            )


__all__ = [
    "GMICloudGateway",
    "GMICloudSettings",
    "classify_provider_error",
    "durable_assets",
    "normalized_gmi_parameters",
]
