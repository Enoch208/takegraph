"""Genblaze/GMI gateway contracts without billable provider calls."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import (
    PromptVisibility,
    ProviderErrorCode,
    RunStatus,
    StepStatus,
)
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.observability.events import (
    PipelineCompletedEvent,
    PipelineStartedEvent,
    StepCompletedEvent,
    StepProgressEvent,
    StreamEvent,
)
from genblaze_core.pipeline.result import PipelineResult
from takegraph_domain.enums import ErrorClass, NodeType
from takegraph_domain.errors import AssetVerificationError, FeatureNotConfiguredError
from takegraph_domain.generation import GenerationEventKind, GenerationInput, GenerationRequest
from takegraph_worker.b2_store import B2Settings
from takegraph_worker.gmi_gateway import (
    GMICloudGateway,
    GMICloudSettings,
    classify_provider_error,
    durable_assets,
    normalized_gmi_parameters,
)

ORG = uuid.UUID("10000000-0000-0000-0000-000000000001")
PROJECT = uuid.UUID("20000000-0000-0000-0000-000000000002")
NODE = uuid.UUID("30000000-0000-0000-0000-000000000003")
ATTEMPT = uuid.UUID("40000000-0000-0000-0000-000000000004")
INPUT = uuid.UUID("50000000-0000-0000-0000-000000000005")


def generation_request(**overrides: object) -> GenerationRequest:
    values: dict[str, object] = {
        "organization_id": ORG,
        "project_id": PROJECT,
        "build_node_id": NODE,
        "attempt_id": ATTEMPT,
        "stable_key": "video.clip.01",
        "node_type": NodeType.VIDEO_GENERATION,
        "provider": "gmicloud",
        "model": "wan2.6-r2v",
        "prompt": "Animate the approved keyframe.",
        "parameters": {"duration_seconds": 4, "aspect_ratio": "16:9"},
        "inputs": (
            GenerationInput(
                asset_id=INPUT,
                url="https://s3.example.invalid/source.png?X-Amz-Signature=temporary",
                media_type="image/png",
                sha256="ab" * 32,
                size_bytes=1024,
            ),
        ),
        "fallback_models": ("pixverse-v5.6-i2v",),
        "idempotency_key": "cd" * 32,
    }
    values.update(overrides)
    return GenerationRequest.model_validate(values)


def b2_settings() -> B2Settings:
    return B2Settings(
        key_id="test-key-id",
        app_key="test-secret",
        bucket="test-bucket",
        region="us-test-1",
        endpoint_url="https://s3.example.invalid",
    )


def settings() -> GMICloudSettings:
    return GMICloudSettings(
        api_key="secret-that-must-not-be-repr-visible",
        image_model="seedream-5.0-pro",
        video_model="wan2.6-r2v",
        video_fallback_model="pixverse-v5.6-i2v",
    )


def completed_result(
    *, url: str = "https://s3.example.invalid/bucket/output.mp4"
) -> PipelineResult:
    step = Step(
        provider="gmicloud",
        model="wan2.6-r2v",
        status=StepStatus.SUCCEEDED,
        assets=[
            Asset(
                url=url,
                media_type="video/mp4",
                sha256="ef" * 32,
                size_bytes=4096,
            )
        ],
    )
    run = Run(
        run_id="60000000-0000-0000-0000-000000000006",
        tenant_id=str(ORG),
        project_id=str(PROJECT),
        status=RunStatus.COMPLETED,
        steps=[step],
    )
    return PipelineResult(run, Manifest.from_run(run))


class TestSettings:
    def test_missing_provider_configuration_fails_loudly(self) -> None:
        with pytest.raises(FeatureNotConfiguredError) as excinfo:
            GMICloudSettings.from_env({"GMI_API_KEY": "present"})
        assert excinfo.value.details["missing"] == [
            "GMI_IMAGE_MODEL",
            "GMI_VIDEO_MODEL",
            "GMI_VIDEO_FALLBACK_MODEL",
        ]

    def test_secret_is_not_exposed_by_repr(self) -> None:
        assert "secret-that-must-not-be-repr-visible" not in repr(settings())

    def test_chat_endpoint_is_rejected_for_media_generation(self) -> None:
        env = {
            "GMI_API_KEY": "key",
            "GMI_IMAGE_MODEL": "image",
            "GMI_VIDEO_MODEL": "video",
            "GMI_VIDEO_FALLBACK_MODEL": "fallback",
            "GMI_BASE_URL": "https://api.gmi-serving.com/v1",
        }
        with pytest.raises(FeatureNotConfiguredError, match="request-queue"):
            GMICloudSettings.from_env(env)


class TestParameterAndErrorMapping:
    def test_duration_is_translated_at_the_provider_boundary(self) -> None:
        assert normalized_gmi_parameters({"duration_seconds": 4, "seed": 2}) == {
            "duration": 4,
            "seed": 2,
        }

    def test_ambiguous_duration_names_are_refused(self) -> None:
        with pytest.raises(ValueError, match="both duration"):
            normalized_gmi_parameters({"duration": 4, "duration_seconds": 4})

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (ProviderErrorCode.TIMEOUT, ErrorClass.TRANSIENT),
            (ProviderErrorCode.SERVER_ERROR, ErrorClass.TRANSIENT),
            (ProviderErrorCode.RATE_LIMIT, ErrorClass.QUOTA),
            (ProviderErrorCode.AUTH_FAILURE, ErrorClass.AUTH),
            (ProviderErrorCode.INVALID_INPUT, ErrorClass.INPUT),
            (ProviderErrorCode.MODEL_ERROR, ErrorClass.MODEL),
            (ProviderErrorCode.CONTENT_POLICY, ErrorClass.POLICY),
            (ProviderErrorCode.UNKNOWN, ErrorClass.INTERNAL),
        ],
    )
    def test_sdk_error_codes_map_without_message_matching(
        self, code: ProviderErrorCode, expected: ErrorClass
    ) -> None:
        assert classify_provider_error(code) is expected


class TestDurableOutput:
    def test_verified_manifest_yields_stored_asset(self) -> None:
        outputs = durable_assets(completed_result())
        assert len(outputs) == 1
        assert outputs[0].sha256 == "ef" * 32
        assert outputs[0].size_bytes == 4096

    def test_presigned_output_is_not_accepted_as_durable_provenance(self) -> None:
        with pytest.raises(AssetVerificationError, match="non-durable asset metadata"):
            durable_assets(
                completed_result(
                    url="https://s3.example.invalid/output.mp4?X-Amz-Signature=temporary"
                )
            )

    def test_manifest_hash_mismatch_blocks_completion(self) -> None:
        result = completed_result()
        result.manifest.canonical_hash = "00" * 32
        with pytest.raises(AssetVerificationError, match="verification failed"):
            durable_assets(result)


class FakeProvider:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePipeline:
    def __init__(self, result: PipelineResult) -> None:
        self.result = result
        self.metadata_values: dict[str, Any] = {}
        self.step_values: dict[str, Any] = {}

    def metadata(self, **values: Any) -> FakePipeline:
        self.metadata_values.update(values)
        return self

    def step(self, provider: Any, **values: Any) -> FakePipeline:
        self.step_values = {"provider": provider, **values}
        return self

    async def astream(self, **values: Any) -> AsyncIterator[StreamEvent]:
        run_id = self.result.run.run_id
        yield PipelineStartedEvent(run_id=run_id, total_steps=1)
        yield StepProgressEvent(
            run_id=run_id,
            step_id=self.result.run.steps[0].step_id,
            provider="gmicloud",
            model="wan2.6-r2v",
            request_id="provider-request-1",
        )
        yield StepProgressEvent(
            run_id=run_id,
            step_id=self.result.run.steps[0].step_id,
            provider="gmicloud",
            model="wan2.6-r2v",
            request_id="provider-request-1",
            progress_pct=0.5,
        )
        yield StepCompletedEvent(
            run_id=run_id,
            step_id=self.result.run.steps[0].step_id,
            step_index=0,
            total_steps=1,
            provider="gmicloud",
            model="wan2.6-r2v",
            elapsed_sec=1.0,
            step=self.result.run.steps[0],
        )
        yield PipelineCompletedEvent(
            run_id=run_id,
            run_status="completed",
            manifest_hash=self.result.manifest.canonical_hash,
            result=self.result,
        )


class FailingPipeline(FakePipeline):
    async def astream(self, **values: Any) -> AsyncIterator[StreamEvent]:
        if False:
            yield PipelineStartedEvent(run_id="unused", total_steps=1)
        raise ProviderError("credential rejected", error_code=ProviderErrorCode.AUTH_FAILURE)


class TestGatewayStream:
    async def test_maps_genblaze_events_and_enforces_durable_completion(self) -> None:
        provider = FakeProvider()
        pipeline = FakePipeline(completed_result())
        gateway = GMICloudGateway(
            settings(),
            b2_settings(),
            pipeline_factory=lambda **_: pipeline,
            provider_factory=lambda _: provider,
            sink_factory=lambda _: object(),
        )

        events = [event async for event in gateway.execute(generation_request())]

        assert [event.kind for event in events] == [
            GenerationEventKind.RUN_STARTED,
            GenerationEventKind.PROVIDER_SUBMITTED,
            GenerationEventKind.PROVIDER_PROGRESS,
            GenerationEventKind.PROVIDER_COMPLETED,
            GenerationEventKind.STORED,
            GenerationEventKind.COMPLETED,
        ]
        assert events[-2].asset is not None
        assert events[-1].manifest_hash == pipeline.result.manifest.canonical_hash
        assert pipeline.step_values["prompt_visibility"] is PromptVisibility.PRIVATE
        assert pipeline.step_values["params"]["duration"] == 4
        assert pipeline.step_values["external_inputs"][0].sha256 == "ab" * 32
        assert provider.closed is True

    async def test_provider_failure_is_a_failed_event_not_false_success(self) -> None:
        provider = FakeProvider()
        gateway = GMICloudGateway(
            settings(),
            b2_settings(),
            pipeline_factory=lambda **_: FailingPipeline(completed_result()),
            provider_factory=lambda _: provider,
            sink_factory=lambda _: object(),
        )

        events = [event async for event in gateway.execute(generation_request())]

        assert [event.kind for event in events] == [GenerationEventKind.FAILED]
        assert events[0].error_class is ErrorClass.AUTH
        assert events[0].error_code == ProviderErrorCode.AUTH_FAILURE.value
        assert provider.closed is True
