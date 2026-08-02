"""ElevenLabs Music-to-B2 boundary without billable provider calls."""

from __future__ import annotations

import hashlib
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from elevenlabs.core.api_error import ApiError
from takegraph_domain.errors import FeatureNotConfiguredError, ProviderAuthError
from takegraph_infrastructure.b2 import StoredObject
from takegraph_worker.elevenlabs_music_gateway import (
    ElevenLabsMusicGateway,
    ElevenLabsMusicSettings,
    MusicGenerationRequest,
)

ORG = uuid.UUID("10000000-0000-0000-0000-000000000001")
PROJECT = uuid.UUID("20000000-0000-0000-0000-000000000002")
NODE = uuid.UUID("30000000-0000-0000-0000-000000000003")
ATTEMPT = uuid.UUID("40000000-0000-0000-0000-000000000004")
MP3 = b"ID3" + b"music-v2-bytes" * 100


def request() -> MusicGenerationRequest:
    return MusicGenerationRequest(
        organization_id=ORG,
        project_id=PROJECT,
        build_node_id=NODE,
        attempt_id=ATTEMPT,
        prompt="Restrained cinematic instrumental bed, no vocals.",
        model="music_v2",
        duration_ms=20_000,
        idempotency_key="ab" * 32,
    )


@dataclass
class FakeResponse:
    headers: dict[str, str]
    data: Any


class FakeContext(AbstractContextManager[FakeResponse]):
    def __init__(self, response: FakeResponse | None = None, failure: Exception | None = None):
        self.response = response
        self.failure = failure

    def __enter__(self) -> FakeResponse:
        if self.failure is not None:
            raise self.failure
        if self.response is None:
            raise RuntimeError("fake response is missing")
        return self.response

    def __exit__(self, *args: object) -> None:
        return None


class FakeComposer:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.values: dict[str, Any] = {}

    def compose(self, **values: Any) -> FakeContext:
        self.values = values
        return FakeContext(
            FakeResponse(headers={"request-id": "music-request-1"}, data=iter((MP3,))),
            self.failure,
        )


class FakeStore:
    prefix = "tenants"

    def __init__(self) -> None:
        self.data: bytes | None = None
        self.key: str | None = None
        self.metadata: dict[str, str] | None = None

    def store_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        self.key = key
        self.data = data
        self.metadata = metadata
        return StoredObject(
            key=key,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            content_type=content_type,
            version_id="version-1",
            deduplicated=False,
        )

    def verify(self, key: str, *, expected_sha256: str) -> bool:
        return (
            key == self.key
            and self.data is not None
            and (hashlib.sha256(self.data).hexdigest() == expected_sha256)
        )


class TestSettings:
    def test_music_v2_is_the_configured_default_output(self) -> None:
        settings = ElevenLabsMusicSettings.from_env(
            {"ELEVENLABS_API_KEY": "secret", "ELEVENLABS_MUSIC_MODEL": "music_v2"}
        )
        assert settings.music_model == "music_v2"
        assert settings.output_format == "mp3_48000_192"
        assert "secret" not in repr(settings)

    def test_unknown_model_fails_loudly(self) -> None:
        with pytest.raises(FeatureNotConfiguredError, match="unsupported"):
            ElevenLabsMusicSettings.from_env(
                {"ELEVENLABS_API_KEY": "secret", "ELEVENLABS_MUSIC_MODEL": "made-up"}
            )


class TestGateway:
    async def test_generated_bytes_are_stored_and_independently_verified(self) -> None:
        composer = FakeComposer()
        store = FakeStore()
        client = SimpleNamespace(
            music=SimpleNamespace(with_raw_response=SimpleNamespace(compose=composer.compose))
        )
        gateway = ElevenLabsMusicGateway(
            ElevenLabsMusicSettings("secret", "music_v2"),
            store,
            client_factory=lambda _: client,
        )

        result = await gateway.generate(request())

        assert result.provider_request_id == "music-request-1"
        assert result.sha256 == hashlib.sha256(MP3).hexdigest()
        assert result.b2_key == store.key
        assert store.data == MP3
        assert store.metadata == {
            "attempt_id": str(ATTEMPT),
            "stable_key": "audio.music",
            "model": "music_v2",
            "idempotency_key": "ab" * 32,
        }
        assert composer.values["force_instrumental"] is True
        assert composer.values["request_options"] == {
            "timeout_in_seconds": 180,
            "max_retries": 0,
        }

    async def test_auth_failure_maps_to_domain_error(self) -> None:
        composer = FakeComposer(failure=ApiError(status_code=401, body={}))
        store = FakeStore()
        client = SimpleNamespace(
            music=SimpleNamespace(with_raw_response=SimpleNamespace(compose=composer.compose))
        )
        gateway = ElevenLabsMusicGateway(
            ElevenLabsMusicSettings("secret", "music_v2"),
            store,
            client_factory=lambda _: client,
        )

        with pytest.raises(ProviderAuthError, match="credential"):
            await gateway.generate(request())
