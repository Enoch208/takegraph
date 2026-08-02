"""Provider-neutral generation boundary (PRD §14.2).

The domain owns these contracts and knows nothing about Genblaze, GMI Cloud,
Backblaze, or provider wire payloads. Adapters translate those systems into this
small vocabulary. In particular, a provider URL is not a successful output:
``DurableGenerationAsset`` requires the SHA-256 and size of bytes stored by
TAKEGRAPH (§8.3.7, §14.5).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from takegraph_domain.canonical import JsonValue
from takegraph_domain.enums import ErrorClass, NodeType


class GenerationEventKind(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    PROVIDER_SUBMITTED = "PROVIDER_SUBMITTED"
    PROVIDER_PROGRESS = "PROVIDER_PROGRESS"
    PROVIDER_RETRY = "PROVIDER_RETRY"
    PROVIDER_COMPLETED = "PROVIDER_COMPLETED"
    STORED = "STORED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReconciliationState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PROVIDER_SUCCEEDED = "PROVIDER_SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NOT_FOUND = "NOT_FOUND"


class CancelState(StrEnum):
    CANCELLED = "CANCELLED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_FOUND = "NOT_FOUND"


class GenerationInput(BaseModel):
    """One already-verified input handed to a provider.

    ``url`` is a short-lived, server-issued HTTPS URL. It is execution input,
    never persisted in a manifest or event because it may contain a signature.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: uuid.UUID
    url: str
    media_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def require_https(self) -> Self:
        if urlsplit(self.url).scheme != "https":
            raise ValueError("generation input URLs must use HTTPS")
        return self


class GenerationRequest(BaseModel):
    """One graph node's one billable provider attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_node_id: uuid.UUID
    attempt_id: uuid.UUID
    stable_key: str = Field(min_length=1, max_length=128)
    node_type: NodeType
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    inputs: tuple[GenerationInput, ...] = ()
    fallback_models: tuple[str, ...] = ()
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: int = Field(default=480, ge=1, le=1800)
    max_retries: int = Field(default=2, ge=0, le=5)
    parent_run_id: str | None = None

    @model_validator(mode="after")
    def require_supported_generation_node(self) -> Self:
        if self.node_type not in (NodeType.IMAGE_GENERATION, NodeType.VIDEO_GENERATION):
            raise ValueError("the GMI generation boundary supports image and video nodes only")
        if self.provider != "gmicloud":
            raise ValueError("a GMI generation request must name provider='gmicloud'")
        return self


class AttemptRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: uuid.UUID
    provider: str
    model: str
    provider_request_id: str = Field(min_length=1)


class DurableGenerationAsset(BaseModel):
    """An output that passed the durable-output gate.

    The URL is deliberately credential-free. Signed URLs are generated only at
    read time and never become provenance (§15.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str
    durable_url: str
    media_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def reject_credential_bearing_url(self) -> Self:
        parsed = urlsplit(self.durable_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("durable asset URLs must be absolute HTTPS URLs")
        if parsed.query:
            raise ValueError("durable asset URLs must not contain query credentials")
        return self


class GenerationEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: GenerationEventKind
    attempt_id: uuid.UUID
    provider: str
    model: str
    run_id: str | None = None
    provider_request_id: str | None = None
    progress: float | None = Field(default=None, ge=0, le=1)
    retry_attempt: int | None = Field(default=None, ge=1)
    message: str | None = None
    asset: DurableGenerationAsset | None = None
    manifest_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_class: ErrorClass | None = None
    error_code: str | None = None


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ReconciliationState
    provider_request_id: str
    message: str | None = None


class CancelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: CancelState
    provider_request_id: str


class GenerationGateway(Protocol):
    """Domain port implemented by the worker's Genblaze adapter."""

    def execute(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]: ...

    async def reconcile(self, attempt: AttemptRef) -> ReconciliationResult: ...

    async def cancel(self, attempt: AttemptRef) -> CancelResult: ...
