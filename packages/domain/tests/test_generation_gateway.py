"""Generation boundary invariants (PRD §14.2 and §14.5)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from takegraph_domain.enums import NodeType
from takegraph_domain.generation import (
    DurableGenerationAsset,
    GenerationInput,
    GenerationRequest,
)

ORG = uuid.UUID("10000000-0000-0000-0000-000000000001")
PROJECT = uuid.UUID("20000000-0000-0000-0000-000000000002")
NODE = uuid.UUID("30000000-0000-0000-0000-000000000003")
ATTEMPT = uuid.UUID("40000000-0000-0000-0000-000000000004")
ASSET = uuid.UUID("50000000-0000-0000-0000-000000000005")


def request(**overrides: object) -> GenerationRequest:
    values: dict[str, object] = {
        "organization_id": ORG,
        "project_id": PROJECT,
        "build_node_id": NODE,
        "attempt_id": ATTEMPT,
        "stable_key": "image.keyframe.01",
        "node_type": NodeType.IMAGE_GENERATION,
        "provider": "gmicloud",
        "model": "seedream-5.0-pro",
        "prompt": "Render the approved ORBIT keyframe.",
        "idempotency_key": "ab" * 32,
    }
    values.update(overrides)
    return GenerationRequest.model_validate(values)


class TestRequestBoundary:
    def test_image_and_video_generation_are_supported(self) -> None:
        assert request().node_type is NodeType.IMAGE_GENERATION
        assert request(node_type=NodeType.VIDEO_GENERATION).node_type is NodeType.VIDEO_GENERATION

    def test_local_composition_cannot_accidentally_call_gmi(self) -> None:
        with pytest.raises(ValidationError, match="image and video"):
            request(node_type=NodeType.MEDIA_COMPOSITION)

    def test_wrong_provider_is_rejected_at_the_gmi_boundary(self) -> None:
        with pytest.raises(ValidationError, match="gmicloud"):
            request(provider="runway")

    def test_billable_submission_requires_an_idempotency_digest(self) -> None:
        with pytest.raises(ValidationError, match="idempotency_key"):
            request(idempotency_key="not-a-hash")

    def test_unknown_parameters_are_rejected(self) -> None:
        values = request().model_dump()
        values["invented_wire_field"] = "must not pass"
        with pytest.raises(ValidationError, match="extra_forbidden"):
            GenerationRequest.model_validate(values)


class TestInputBoundary:
    def test_verified_https_input_is_accepted(self) -> None:
        item = GenerationInput(
            asset_id=ASSET,
            url="https://example.invalid/signed-source.png?X-Amz-Signature=temporary",
            media_type="image/png",
            sha256="cd" * 32,
            size_bytes=1024,
        )
        assert item.sha256 == "cd" * 32

    def test_non_https_input_is_rejected_before_provider_submission(self) -> None:
        with pytest.raises(ValidationError, match="HTTPS"):
            GenerationInput(
                asset_id=ASSET,
                url="file:///tmp/source.png",
                media_type="image/png",
                sha256="cd" * 32,
                size_bytes=1024,
            )


class TestDurableOutputGate:
    def test_stored_hash_size_and_credential_free_url_are_required(self) -> None:
        asset = DurableGenerationAsset(
            asset_id=str(ASSET),
            durable_url="https://s3.example.invalid/bucket/tenants/org/cas/output.png",
            media_type="image/png",
            sha256="ef" * 32,
            size_bytes=4096,
        )
        assert asset.size_bytes == 4096

    def test_url_only_provider_output_cannot_be_called_durable(self) -> None:
        with pytest.raises(ValidationError):
            DurableGenerationAsset.model_validate(
                {
                    "asset_id": str(ASSET),
                    "durable_url": "https://provider.invalid/output.png",
                    "media_type": "image/png",
                }
            )

    def test_presigned_url_cannot_be_persisted_as_provenance(self) -> None:
        with pytest.raises(ValidationError, match="query credentials"):
            DurableGenerationAsset(
                asset_id=str(ASSET),
                durable_url="https://s3.example.invalid/output.png?X-Amz-Signature=secret",
                media_type="image/png",
                sha256="ef" * 32,
                size_bytes=4096,
            )
