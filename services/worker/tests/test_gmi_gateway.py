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
from takegraph_infrastructure.b2 import B2Settings
from takegraph_worker.gmi_gateway import (
    GMICloudGateway,
    GMICloudSettings,
    classify_provider_error,
    durable_assets,
    normalized_gmi_parameters,
    provider_parameters,
    redact_provider_message,
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
        "model": "pixverse-v6-i2v",
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
        "fallback_models": ("kling-v3-image-to-video",),
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
        video_model="pixverse-v6-i2v",
        video_fallback_model="kling-v3-image-to-video",
    )


def completed_result(
    *, url: str = "https://s3.example.invalid/bucket/output.mp4"
) -> PipelineResult:
    step = Step(
        provider="gmicloud",
        model="pixverse-v6-i2v",
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

    def test_pixverse_v6_uses_live_i2v_parameter_contract(self) -> None:
        assert provider_parameters(generation_request()) == {
            "duration": 4,
            "quality": "720p",
        }

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


class FakeSink:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write_run(self, run: Any, manifest: Any) -> None:
        self.written.append(run.run_id)

    def close(self) -> None:
        return None


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


class SubmittedThenFailingPipeline(FakePipeline):
    async def astream(self, **values: Any) -> AsyncIterator[StreamEvent]:
        config = values["_config_override"]
        yield PipelineStartedEvent(run_id=self.result.run.run_id, total_steps=1)
        config["on_submit"]("step-1", "provider-request-9")
        raise ProviderError(
            "Poll timeout after 100.0s (limit: 480.0s)",
            error_code=ProviderErrorCode.TIMEOUT,
        )


class RecoveringProvider(FakeProvider):
    def __init__(self, step: Step) -> None:
        super().__init__()
        self.step = step
        self.resumed: list[str] = []

    async def aresume(self, prediction_id: Any, step: Step, config: Any) -> Step:
        self.resumed.append(str(prediction_id))
        return self.step


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
        assert events[0].message == "credential rejected"
        assert provider.closed is True


class TestPaidWorkRecovery:
    @staticmethod
    def _gateway(provider: FakeProvider, pipeline: FakePipeline) -> GMICloudGateway:
        return GMICloudGateway(
            settings(),
            b2_settings(),
            pipeline_factory=lambda **_: pipeline,
            provider_factory=lambda _: provider,
            sink_factory=lambda _: FakeSink(),
        )

    async def test_completed_provider_job_is_recovered_instead_of_lost(self) -> None:
        provider = RecoveringProvider(completed_result().run.steps[0])
        gateway = self._gateway(provider, SubmittedThenFailingPipeline(completed_result()))

        events = [event async for event in gateway.execute(generation_request())]

        assert [event.kind for event in events] == [
            GenerationEventKind.RUN_STARTED,
            GenerationEventKind.PROVIDER_SUBMITTED,
            GenerationEventKind.PROVIDER_RECOVERED,
            GenerationEventKind.STORED,
            GenerationEventKind.COMPLETED,
        ]
        assert provider.resumed == ["provider-request-9"]
        assert events[1].provider_request_id == "provider-request-9"
        assert events[2].message == "Poll timeout after 100.0s (limit: 480.0s)"
        assert events[3].asset is not None
        assert events[3].asset.sha256 == "ef" * 32
        assert provider.closed is True

    async def test_unrecoverable_job_reports_the_real_provider_error(self) -> None:
        failed = completed_result().run.steps[0]
        failed.status = StepStatus.FAILED
        failed.assets = []
        provider = RecoveringProvider(failed)
        gateway = self._gateway(provider, SubmittedThenFailingPipeline(completed_result()))

        events = [event async for event in gateway.execute(generation_request())]

        assert [event.kind for event in events] == [
            GenerationEventKind.RUN_STARTED,
            GenerationEventKind.PROVIDER_SUBMITTED,
            GenerationEventKind.FAILED,
        ]
        assert events[-1].error_class is ErrorClass.TRANSIENT
        assert events[-1].message == "Poll timeout after 100.0s (limit: 480.0s)"

    async def test_known_provider_request_is_resumed_before_paying_again(self) -> None:
        provider = RecoveringProvider(completed_result().run.steps[0])
        pipeline = FakePipeline(completed_result())
        gateway = self._gateway(provider, pipeline)

        events = [
            event
            async for event in gateway.execute(
                generation_request(resume_provider_request_id="already-paid-7")
            )
        ]

        assert [event.kind for event in events] == [
            GenerationEventKind.PROVIDER_RECOVERED,
            GenerationEventKind.STORED,
            GenerationEventKind.COMPLETED,
        ]
        assert provider.resumed == ["already-paid-7"]
        assert pipeline.step_values == {}

    def test_signed_input_urls_are_redacted_from_provider_errors(self) -> None:
        message = redact_provider_message(
            "submit failed: https://b2.example/asset.jpg"
            "?X-Amz-Credential=005d1e&X-Amz-Signature=08f26fede170f2c6&token=abc"
        )
        assert "08f26fede170f2c6" not in message
        assert "005d1e" not in message
        assert message.count("[REDACTED]") == 3
