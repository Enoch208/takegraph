"""Genblaze/ElevenLabs narration contracts without billable provider calls."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from genblaze_core.exceptions import PipelineError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import PromptVisibility, ProviderErrorCode, RunStatus, StepStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.pipeline.result import PipelineResult
from takegraph_domain.errors import FeatureNotConfiguredError, ProviderAuthError
from takegraph_infrastructure.b2 import B2Settings
from takegraph_worker.elevenlabs_gateway import (
    ElevenLabsNarrationGateway,
    ElevenLabsSettings,
    NarrationRequest,
)

ORG = uuid.UUID("10000000-0000-0000-0000-000000000001")
PROJECT = uuid.UUID("20000000-0000-0000-0000-000000000002")
NODE = uuid.UUID("30000000-0000-0000-0000-000000000003")
ATTEMPT = uuid.UUID("40000000-0000-0000-0000-000000000004")


def settings() -> ElevenLabsSettings:
    return ElevenLabsSettings(
        api_key="secret-that-must-not-be-repr-visible",
        tts_model="eleven_multilingual_v2",
        voice_id="voice-test",
    )


def b2_settings() -> B2Settings:
    return B2Settings(
        key_id="key-id",
        app_key="app-key",
        bucket="work-bucket",
        region="us-test-1",
        endpoint_url="https://s3.example.invalid",
    )


def request() -> NarrationRequest:
    return NarrationRequest(
        organization_id=ORG,
        project_id=PROJECT,
        build_node_id=NODE,
        attempt_id=ATTEMPT,
        text="ORBIT launches with no added sugar.",
        model="eleven_multilingual_v2",
        idempotency_key="ab" * 32,
    )


def completed_result(*, error_code: ProviderErrorCode | None = None) -> PipelineResult:
    failed = error_code is not None
    step = Step(
        provider="elevenlabs-tts",
        model="eleven_multilingual_v2",
        status=StepStatus.FAILED if failed else StepStatus.SUCCEEDED,
        error="provider failure" if failed else None,
        error_code=error_code,
        assets=(
            []
            if failed
            else [
                Asset(
                    url="https://s3.example.invalid/work-bucket/narration.mp3",
                    media_type="audio/mpeg",
                    sha256="cd" * 32,
                    size_bytes=2_048,
                )
            ]
        ),
    )
    run = Run(
        run_id="50000000-0000-0000-0000-000000000005",
        tenant_id=str(ORG),
        project_id=str(PROJECT),
        status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
        steps=[step],
    )
    return PipelineResult(run, Manifest.from_run(run))


class FakeProvider:
    pass


class FakePipeline:
    def __init__(self, result: PipelineResult, *, failure: Exception | None = None) -> None:
        self.result = result
        self.failure = failure
        self.metadata_values: dict[str, Any] = {}
        self.step_values: dict[str, Any] = {}
        self.run_values: dict[str, Any] = {}

    def metadata(self, **values: Any) -> FakePipeline:
        self.metadata_values.update(values)
        return self

    def step(self, provider: Any, **values: Any) -> FakePipeline:
        self.step_values = {"provider": provider, **values}
        return self

    async def arun(self, **values: Any) -> PipelineResult:
        self.run_values = values
        if self.failure is not None:
            raise self.failure
        return self.result


class TestSettings:
    def test_missing_tts_configuration_fails_loudly(self) -> None:
        with pytest.raises(FeatureNotConfiguredError) as excinfo:
            ElevenLabsSettings.from_env({"ELEVENLABS_API_KEY": "present"})
        assert excinfo.value.details["missing"] == [
            "ELEVENLABS_TTS_MODEL",
            "ELEVENLABS_VOICE_ID",
        ]

    def test_secret_is_not_exposed_by_repr(self) -> None:
        assert "secret-that-must-not-be-repr-visible" not in repr(settings())


class TestGateway:
    async def test_pipeline_uses_private_prompt_and_returns_durable_asset(self) -> None:
        pipeline = FakePipeline(completed_result())
        provider = FakeProvider()
        gateway = ElevenLabsNarrationGateway(
            settings(),
            b2_settings(),
            pipeline_factory=lambda **_: pipeline,
            provider_factory=lambda: provider,
            sink_factory=lambda _: object(),
        )

        result = await gateway.generate(request())

        assert result.asset.sha256 == "cd" * 32
        assert result.asset.size_bytes == 2_048
        assert pipeline.step_values["provider"] is provider
        assert pipeline.step_values["prompt_visibility"] is PromptVisibility.PRIVATE
        assert pipeline.step_values["params"] == {
            "voice_id": "voice-test",
            "output_format": "mp3_44100_128",
            "with_timestamps": True,
        }
        assert pipeline.run_values["max_retries"] == 0
        assert pipeline.run_values["raise_on_failure"] is True
        assert pipeline.metadata_values["idempotency_key"] == request().idempotency_key

    async def test_pipeline_auth_failure_maps_to_typed_provider_error(self) -> None:
        failed = completed_result(error_code=ProviderErrorCode.AUTH_FAILURE)
        error = PipelineError(
            "failed",
            result=failed,
            failed_step_index=0,
            failed_step_error="credential rejected",
        )
        pipeline = FakePipeline(failed, failure=error)
        gateway = ElevenLabsNarrationGateway(
            settings(),
            b2_settings(),
            pipeline_factory=lambda **_: pipeline,
            provider_factory=FakeProvider,
            sink_factory=lambda _: object(),
        )

        with pytest.raises(ProviderAuthError, match="credential"):
            await gateway.generate(request())
