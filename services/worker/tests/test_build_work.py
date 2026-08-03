"""Crash boundaries for incremental build-node execution on real PostgreSQL."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from PIL import Image, ImageDraw
from sqlalchemy import select, text, update
from takegraph_api.db.models import (
    Asset,
    Attempt,
    AttemptAsset,
    AttemptEvent,
    Build,
    BuildNode,
    DomainEvent,
    GraphNode,
    Organization,
    Project,
    ProjectRevision,
    Source,
    SourceVersion,
    Validation,
    WorkItem,
)
from takegraph_api.graph_persistence import OrbitGraphRepository
from takegraph_api.projects import ProjectCreateRequest, ProjectService
from takegraph_api.queue import WorkQueue
from takegraph_domain.auth import Principal
from takegraph_domain.canonical import canonical_hash
from takegraph_domain.enums import (
    AttemptMechanism,
    AttemptStatus,
    BuildNodeStatus,
    BuildStatus,
    Role,
)
from takegraph_domain.errors import AssetVerificationError
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.generation import (
    AttemptRef,
    CancelResult,
    DurableGenerationAsset,
    GenerationEvent,
    GenerationEventKind,
    GenerationRequest,
    ReconciliationResult,
)
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    DEFAULT_LEGAL_LINE,
    EXPECTED_LEGAL_COPY_REBUILD,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
)
from takegraph_infrastructure.b2 import StoredObject
from takegraph_infrastructure.delivery import DeliveryArtifact, DeliveryInput
from takegraph_infrastructure.media import MediaProbe
from takegraph_worker.anthropic_gateway import (
    CopyGenerationRequest,
    CopyGenerationResult,
    CopyPack,
)
from takegraph_worker.anthropic_plan_gateway import (
    PlanGenerationRequest,
    PlanGenerationResult,
    Shot,
    ShotPlan,
)
from takegraph_worker.build_work import BuildWorkHandlers
from takegraph_worker.delivery_work import DeliveryWorkHandlers
from takegraph_worker.elevenlabs_gateway import NarrationRequest, NarrationResult
from takegraph_worker.elevenlabs_music_gateway import (
    MusicGenerationRequest,
    MusicGenerationResult,
)
from takegraph_worker.end_card_work import EndCardWorkHandlers
from takegraph_worker.gmi_work import GMIWorkHandlers
from takegraph_worker.local_image_work import LocalImageWorkHandlers
from takegraph_worker.music_work import (
    MUSIC_PROMPT_LIMIT,
    MusicWorkHandlers,
    _compose_prompt,
)
from takegraph_worker.narration_work import NarrationWorkHandlers
from takegraph_worker.plan_work import PlanWorkHandlers
from takegraph_worker.runtime import WorkerRuntime

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _product_png() -> bytes:
    image = Image.new("RGB", (240, 320), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80, 30, 160, 290), radius=24, fill="#111820")
    draw.rectangle((93, 118, 147, 205), fill="#F5F7FA")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


PRODUCT_PNG = _product_png()


class MemoryStore:
    bucket = "takegraph-work-test"
    prefix = "tenants"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.verify_calls = 0

    def store_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        del metadata
        digest = hashlib.sha256(data).hexdigest()
        deduplicated = key in self.objects
        self.objects.setdefault(key, data)
        return StoredObject(
            key=key,
            sha256=digest,
            size_bytes=len(data),
            content_type=content_type,
            version_id="memory-v1",
            deduplicated=deduplicated,
        )

    def verify(self, key: str, *, expected_sha256: str) -> bool:
        self.verify_calls += 1
        data = self.objects.get(key)
        return data is not None and hashlib.sha256(data).hexdigest() == expected_sha256

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def get_verified(self, key: str, *, expected_sha256: str) -> bytes:
        # Routed through verify() so subclasses that override verify to simulate
        # a storage fault still exercise that fault through this path.
        if not self.verify(key, expected_sha256=expected_sha256):
            raise AssetVerificationError(f"Stored bytes for {key} did not match {expected_sha256}.")
        return self.get_bytes(key)

    def key_from_url(self, url: str) -> str | None:
        prefix = "https://memory.invalid/"
        return url[len(prefix) :] if url.startswith(prefix) else None

    def presign_get(self, key: str, *, ttl_seconds: int = 900) -> str:
        assert ttl_seconds > 0
        return f"https://memory.invalid/{key}?temporary=test"


class FlakyMusicStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.remaining_music_verify_failures = 1

    def verify(self, key: str, *, expected_sha256: str) -> bool:
        if key.endswith(".mp3") and self.remaining_music_verify_failures:
            self.remaining_music_verify_failures -= 1
            return False
        return super().verify(key, expected_sha256=expected_sha256)


class FakeCopyGenerator:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[CopyGenerationRequest] = []
        self.failure = failure

    async def generate(self, request: CopyGenerationRequest) -> CopyGenerationResult:
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        return _provider_result(required_line=request.required_legal_line, model=request.model)


class FakeNarrationGenerator:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.calls: list[NarrationRequest] = []

    async def generate(self, request: NarrationRequest) -> NarrationResult:
        self.calls.append(request)
        return _narration_result(self.store, request.organization_id)


class FakeMusicGenerator:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.calls: list[MusicGenerationRequest] = []

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        self.calls.append(request)
        return _music_result(self.store, request.organization_id, model=request.model)


class FakePlanGenerator:
    def __init__(self) -> None:
        self.calls: list[PlanGenerationRequest] = []

    async def generate(self, request: PlanGenerationRequest) -> PlanGenerationResult:
        self.calls.append(request)
        return _plan_result(model=request.model)


class FakeGMIGateway:
    def __init__(self, store: MemoryStore, output: bytes = PNG_1X1) -> None:
        self.store = store
        self.output = output
        self.calls: list[GenerationRequest] = []

    async def execute(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        self.calls.append(request)
        digest = hashlib.sha256(self.output).hexdigest()
        suffix = "png" if request.stable_key.startswith("image.") else "mp4"
        media_type = "image/png" if suffix == "png" else "video/mp4"
        key = f"tenants/{request.organization_id}/genblaze/{digest}.{suffix}"
        self.store.objects[key] = self.output
        base = {
            "attempt_id": request.attempt_id,
            "provider": request.provider,
            "model": request.model,
            "run_id": "run_test_gmi",
        }
        yield GenerationEvent(kind=GenerationEventKind.RUN_STARTED, **base)
        yield GenerationEvent(
            kind=GenerationEventKind.PROVIDER_SUBMITTED,
            provider_request_id="request_test_gmi",
            **base,
        )
        yield GenerationEvent(
            kind=GenerationEventKind.PROVIDER_PROGRESS,
            provider_request_id="request_test_gmi",
            progress=0.5,
            **base,
        )
        yield GenerationEvent(
            kind=GenerationEventKind.PROVIDER_COMPLETED,
            provider_request_id="request_test_gmi",
            **base,
        )
        yield GenerationEvent(
            kind=GenerationEventKind.STORED,
            asset=DurableGenerationAsset(
                asset_id="provider-output",
                durable_url=f"https://memory.invalid/{key}",
                media_type=media_type,
                sha256=digest,
                size_bytes=len(self.output),
            ),
            **base,
        )
        yield GenerationEvent(
            kind=GenerationEventKind.COMPLETED,
            manifest_hash=digest,
            **base,
        )

    async def reconcile(self, attempt: AttemptRef) -> ReconciliationResult:
        raise AssertionError(f"unexpected reconciliation for {attempt.attempt_id}")

    async def cancel(self, attempt: AttemptRef) -> CancelResult:
        raise AssertionError(f"unexpected cancellation for {attempt.attempt_id}")


class FakeDeliveryComposer:
    def __init__(self) -> None:
        self.calls: list[DeliveryInput] = []

    def __call__(self, source: DeliveryInput, *, temp_root) -> tuple[DeliveryArtifact, ...]:
        assert temp_root.is_absolute()
        self.calls.append(source)
        specs = (
            ("master_16x9", "master_16x9.mp4", "video/mp4", "VIDEO"),
            ("master_9x16", "master_9x16.mp4", "video/mp4", "VIDEO"),
            ("final_audio", "final_audio.wav", "audio/wav", "AUDIO"),
            ("thumbnail_16x9", "thumbnail_16x9.jpg", "image/jpeg", "IMAGE"),
            ("thumbnail_9x16", "thumbnail_9x16.jpg", "image/jpeg", "IMAGE"),
            ("captions", "captions.vtt", "text/vtt", "DOCUMENT"),
            ("report", "report.json", "application/json", "DOCUMENT"),
        )
        return tuple(
            DeliveryArtifact(role, filename, mime, kind, role.encode(), {"verified": True})
            for role, filename, mime, kind in specs
        )


@dataclass(frozen=True, slots=True)
class IncrementalBuild:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    copy_node_id: uuid.UUID
    model: str


def _provider_result(*, required_line: str = "no added sugar", model: str = "test-model"):
    return CopyGenerationResult(
        provider_message_id="msg_test_copy",
        model=model,
        input_tokens=31,
        output_tokens=22,
        output=CopyPack(
            legal_line=required_line,
            narration=f"ORBIT launches with {required_line}.",
            captions=("A cleaner launch", "Made for motion"),
        ),
    )


def _narration_result(store: MemoryStore, organization_id: uuid.UUID) -> NarrationResult:
    raw = b"provider wav bytes"
    digest = hashlib.sha256(raw).hexdigest()
    key = f"tenants/{organization_id}/genblaze/{digest}.wav"
    store.objects[key] = raw
    return NarrationResult.model_validate(
        {
            "run_id": "genblaze-run-narration",
            "manifest_hash": "ef" * 32,
            "asset": {
                "asset_id": "raw-narration",
                "durable_url": f"https://memory.invalid/{key}",
                "media_type": "audio/wav",
                "sha256": digest,
                "size_bytes": len(raw),
            },
        }
    )


def _normalize_narration(_: bytes) -> tuple[bytes, MediaProbe]:
    return b"normalized 48khz mono wav", MediaProbe(
        media_kind="AUDIO",
        format_name="wav",
        codec_names=("pcm_s16le",),
        width=None,
        height=None,
        duration_ms=1_200,
        frame_rate=None,
        has_audio=True,
        sample_rate=48_000,
        channels=1,
    )


def _music_result(
    store: MemoryStore,
    organization_id: uuid.UUID,
    *,
    model: str = "music_v2",
) -> MusicGenerationResult:
    payload = b"ID3" + b"orbit music bytes" * 100
    digest = hashlib.sha256(payload).hexdigest()
    key = f"tenants/{organization_id}/cas/sha256/{digest}.mp3"
    store.objects[key] = payload
    return MusicGenerationResult(
        provider_request_id=None,
        model=model,
        b2_key=key,
        sha256=digest,
        size_bytes=len(payload),
        media_type="audio/mpeg",
    )


def _probe_music(_: bytes) -> MediaProbe:
    return MediaProbe(
        media_kind="AUDIO",
        format_name="mp3",
        codec_names=("mp3",),
        width=None,
        height=None,
        duration_ms=20_000,
        frame_rate=None,
        has_audio=True,
        sample_rate=48_000,
        channels=2,
    )


def _plan_result(*, model: str = "test-plan-model") -> PlanGenerationResult:
    shots = tuple(
        Shot(
            index=index,
            title=f"ORBIT shot {index}",
            visual_direction=f"Bottle composition {index} on graphite.",
            camera="50mm controlled dolly",
            motion="restrained orbital move",
            duration_seconds=4,
        )
        for index in range(1, 5)
    )
    return PlanGenerationResult(
        provider_message_id="msg_test_plan",
        model=model,
        output=ShotPlan(shots=shots),
        input_tokens=120,
        output_tokens=240,
    )


async def _seed_incremental_build(session) -> IncrementalBuild:
    organization_id = uuid.uuid4()
    principal = Principal(
        actor_id=uuid.uuid4(),
        subject="build-worker-test",
        organization_id=organization_id,
        role=Role.OWNER,
    )
    session.add(
        Organization(
            id=organization_id,
            slug=f"build-worker-{uuid.uuid4().hex}",
            name="Build Worker Test",
        )
    )
    await session.flush()
    created = await ProjectService(session).create(
        principal=principal,
        request=ProjectCreateRequest(
            slug=f"orbit-worker-{uuid.uuid4().hex[:12]}",
            name="ORBIT",
            spec={
                "parameters": {
                    PARAM_LEGAL_LINE: DEFAULT_LEGAL_LINE,
                    PARAM_BRIEF_TEXT: DEFAULT_BRIEF_TEXT,
                }
            },
        ),
    )
    parent = await session.get(ProjectRevision, created.active_revision_id)
    assert parent is not None
    spec = copy.deepcopy(parent.spec_json)
    parameters = spec["parameters"]
    assert isinstance(parameters, dict)
    parameters[PARAM_LEGAL_LINE] = "no added sugar"
    revision = ProjectRevision(
        id=uuid.uuid4(),
        project_id=created.id,
        revision_no=2,
        parent_revision_id=parent.id,
        spec_json=spec,
        canonical_hash=canonical_hash(spec),
        created_by=principal.actor_id,
    )
    session.add(revision)
    await session.flush()
    persisted = await OrbitGraphRepository(session).compile_revision(revision.id)
    await session.execute(
        update(Project).where(Project.id == created.id).values(active_revision_id=revision.id)
    )

    graph_nodes = (
        await session.scalars(select(GraphNode).where(GraphNode.graph_revision_id == persisted.id))
    ).all()
    build = Build(
        id=uuid.uuid4(),
        project_id=created.id,
        project_revision_id=revision.id,
        graph_revision_id=persisted.id,
        status=str(BuildStatus.QUEUED),
        total_nodes=18,
        reused_nodes=14,
        rebuilt_nodes=4,
        is_fixture=False,
        version=1,
    )
    session.add(build)
    copy_node_id: uuid.UUID | None = None
    for graph_node in graph_nodes:
        if graph_node.stable_key == "copy.pack":
            status = BuildNodeStatus.QUEUED
        elif graph_node.stable_key in EXPECTED_LEGAL_COPY_REBUILD:
            status = BuildNodeStatus.PENDING
        else:
            status = BuildNodeStatus.REUSED
        node_id = uuid.uuid4()
        if graph_node.stable_key == "copy.pack":
            copy_node_id = node_id
        session.add(
            BuildNode(
                id=node_id,
                build_id=build.id,
                graph_node_id=graph_node.id,
                stable_key=graph_node.stable_key,
                fingerprint=canonical_hash(
                    {"build": str(build.id), "stable_key": graph_node.stable_key}
                ),
                status=str(status),
                resolution=("EXACT_VALIDATED_REUSE" if status is BuildNodeStatus.REUSED else None),
                selected_asset_set_hash=(
                    canonical_hash({"reused": graph_node.stable_key})
                    if status is BuildNodeStatus.REUSED
                    else None
                ),
                version=1,
            )
        )
    assert copy_node_id is not None
    await session.flush()
    await WorkQueue(session).enqueue(
        kind="EXECUTE_BUILD_NODE",
        target_id=copy_node_id,
        build_id=build.id,
        priority=80,
        dedupe_key=f"execute:{copy_node_id}",
        payload={"stable_key": "copy.pack", "trigger_source": "APPLICATION_COMMIT"},
    )
    await session.commit()
    return IncrementalBuild(
        organization_id=organization_id,
        project_id=created.id,
        build_id=build.id,
        copy_node_id=copy_node_id,
        model="test-model",
    )


def _runtime(session_factory, store: MemoryStore, generator: FakeCopyGenerator) -> WorkerRuntime:
    handlers = BuildWorkHandlers(
        session_factory,
        store,  # type: ignore[arg-type] - complete in-memory contract for this handler
        generator=generator,
        environment={"EVALUATOR_MODEL": "test-model"},
    )
    return WorkerRuntime(
        session_factory,
        store,  # type: ignore[arg-type] - source handlers are not called in this test
        owner="build-worker-test",
        lease_seconds=30,
        heartbeat_seconds=5,
        concurrency=1,
        build_handlers=handlers,
    )


async def _seed_attempt(session, seeded: IncrementalBuild, status: AttemptStatus) -> uuid.UUID:
    node = await session.get(BuildNode, seeded.copy_node_id)
    build = await session.get(Build, seeded.build_id)
    assert node is not None and build is not None
    node.status = str(BuildNodeStatus.RUNNING)
    build.status = str(BuildStatus.RUNNING)
    attempt_id = uuid.uuid4()
    session.add(
        Attempt(
            id=attempt_id,
            build_node_id=node.id,
            attempt_no=1,
            mechanism=str(AttemptMechanism.PRIMARY),
            provider="anthropic",
            model=seeded.model,
            idempotency_key=submission_idempotency_key(
                build_node_id=node.id,
                fingerprint=node.fingerprint,
                mechanism=AttemptMechanism.PRIMARY,
                provider="anthropic",
                model=seeded.model,
            ),
            status=str(status),
        )
    )
    if status is AttemptStatus.FETCHING:
        session.add(
            AttemptEvent(
                attempt_id=attempt_id,
                provider_event_type="attempt.fetching",
                provider_event_json=_provider_result(model=seeded.model).model_dump(mode="json"),
            )
        )
    await session.commit()
    return attempt_id


async def _seed_narration_attempt(
    session,
    seeded: IncrementalBuild,
    store: MemoryStore,
    status: AttemptStatus,
) -> tuple[uuid.UUID, uuid.UUID]:
    node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == "audio.narration",
        )
    )
    assert node is not None
    node.status = str(BuildNodeStatus.RUNNING)
    attempt_id = uuid.uuid4()
    model = "eleven_multilingual_v2"
    attempt = Attempt(
        id=attempt_id,
        build_node_id=node.id,
        attempt_no=1,
        mechanism=str(AttemptMechanism.PRIMARY),
        provider="elevenlabs",
        model=model,
        idempotency_key=submission_idempotency_key(
            build_node_id=node.id,
            fingerprint=node.fingerprint,
            mechanism=AttemptMechanism.PRIMARY,
            provider="elevenlabs",
            model=model,
        ),
        status=str(status),
    )
    session.add(attempt)
    if status is AttemptStatus.FETCHING:
        session.add(
            AttemptEvent(
                attempt_id=attempt_id,
                provider_event_type="attempt.fetching",
                provider_event_json=_narration_result(store, seeded.organization_id).model_dump(
                    mode="json"
                ),
            )
        )
    await session.commit()
    return node.id, attempt_id


async def _cleanup(session, seeded: IncrementalBuild) -> None:
    params = {
        "project_id": seeded.project_id,
        "organization_id": seeded.organization_id,
    }
    statements = (
        """delete from work_items where build_id in
           (select id from builds where project_id=:project_id)""",
        "delete from domain_events where project_id=:project_id",
        """delete from validations where build_node_id in
           (select id from build_nodes where build_id in
             (select id from builds where project_id=:project_id))""",
        """delete from attempt_assets where attempt_id in
           (select id from attempts where build_node_id in
             (select id from build_nodes where build_id in
               (select id from builds where project_id=:project_id)))""",
        """delete from attempt_events where attempt_id in
           (select id from attempts where build_node_id in
             (select id from build_nodes where build_id in
               (select id from builds where project_id=:project_id)))""",
        """delete from attempts where build_node_id in
           (select id from build_nodes where build_id in
             (select id from builds where project_id=:project_id))""",
        """delete from build_nodes where build_id in
           (select id from builds where project_id=:project_id)""",
        "delete from builds where project_id=:project_id",
        """delete from source_versions where source_id in
           (select id from sources where project_id=:project_id)""",
        "delete from sources where project_id=:project_id",
        """delete from graph_edges where graph_revision_id in
           (select gr.id from graph_revisions gr
            join project_revisions pr on pr.id=gr.project_revision_id
            where pr.project_id=:project_id)""",
        """delete from graph_nodes where graph_revision_id in
           (select gr.id from graph_revisions gr
            join project_revisions pr on pr.id=gr.project_revision_id
            where pr.project_id=:project_id)""",
        """delete from graph_revisions where project_revision_id in
           (select id from project_revisions where project_id=:project_id)""",
        "update projects set active_revision_id=null where id=:project_id",
        "delete from project_revisions where project_id=:project_id",
        "delete from projects where id=:project_id",
        "delete from assets where organization_id=:organization_id",
        "delete from organizations where id=:organization_id",
    )
    for statement in statements:
        await session.execute(text(statement), params)
    await session.commit()


async def test_fresh_copy_execution_persists_bytes_validation_and_ready_children(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakeCopyGenerator()
    try:
        receipt = await _runtime(session_factory, store, generator).run_once()
        assert receipt.completed == 1
        assert len(generator.calls) == 1
        assert generator.calls[0].required_legal_line == "no added sugar"
        assert store.verify_calls == 1

        session.expire_all()
        copy_node = await session.get(BuildNode, seeded.copy_node_id)
        assert copy_node is not None and copy_node.status == "PASSED"
        attempt = await session.scalar(
            select(Attempt).where(Attempt.build_node_id == seeded.copy_node_id)
        )
        assert attempt is not None and attempt.status == "SUCCEEDED"
        validations = (
            await session.scalars(
                select(Validation).where(Validation.build_node_id == seeded.copy_node_id)
            )
        ).all()
        assert {(row.gate_key, row.status) for row in validations} == {
            ("required_phrase", "PASS"),
            ("superseded_phrase", "PASS"),
            ("schema", "PASS"),
        }
        queued = (
            await session.scalars(
                select(BuildNode.stable_key).where(
                    BuildNode.build_id == seeded.build_id,
                    BuildNode.status == "QUEUED",
                )
            )
        ).all()
        assert set(queued) == {"audio.narration", "graphic.end_card"}
        assert (
            await session.scalar(
                select(BuildNode.status).where(
                    BuildNode.build_id == seeded.build_id,
                    BuildNode.stable_key == "compose.delivery_package",
                )
            )
            == "PENDING"
        )
        assert (
            await session.scalar(
                select(WorkItem.status).where(WorkItem.target_id == seeded.copy_node_id)
            )
            == "DONE"
        )
        node_events = (
            await session.scalars(
                select(DomainEvent).where(
                    DomainEvent.build_id == seeded.build_id,
                    DomainEvent.event_type == "build.node.status_changed",
                )
            )
        ).all()
        assert {event.payload_json["to"] for event in node_events} >= {
            "RUNNING",
            "STORING",
            "VERIFYING",
            "PASSED",
            "QUEUED",
        }
        assert all(event.payload_json["from"] != event.payload_json["to"] for event in node_events)
    finally:
        await _cleanup(session, seeded)


async def test_fetching_attempt_recovers_without_second_provider_call(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    await _seed_attempt(session, seeded, AttemptStatus.FETCHING)
    store = MemoryStore()
    generator = FakeCopyGenerator()
    try:
        receipt = await _runtime(session_factory, store, generator).run_once()
        assert receipt.completed == 1
        assert generator.calls == []
        session.expire_all()
        node = await session.get(BuildNode, seeded.copy_node_id)
        assert node is not None and node.status == "PASSED"
    finally:
        await _cleanup(session, seeded)


async def test_submitting_attempt_is_not_resubmitted_and_requires_review(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    attempt_id = await _seed_attempt(session, seeded, AttemptStatus.SUBMITTING)
    generator = FakeCopyGenerator()
    try:
        receipt = await _runtime(session_factory, MemoryStore(), generator).run_once()
        assert receipt.completed == 1
        assert generator.calls == []
        session.expire_all()
        attempt = await session.get(Attempt, attempt_id)
        node = await session.get(BuildNode, seeded.copy_node_id)
        build = await session.get(Build, seeded.build_id)
        assert attempt is not None and attempt.error_code == "AMBIGUOUS_SUBMISSION"
        assert attempt.status == "FAILED"
        assert node is not None and node.status == "WAITING_REVIEW"
        assert build is not None and build.status == "WAITING_REVIEW"
    finally:
        await _cleanup(session, seeded)


async def test_narration_normalizes_provider_output_and_persists_four_gates(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    copy_generator = FakeCopyGenerator()
    narration_generator = FakeNarrationGenerator(store)

    try:
        first = await _runtime(session_factory, store, copy_generator).run_once()
        assert first.completed == 1
        narration_node_id = await session.scalar(
            select(BuildNode.id).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "audio.narration",
            )
        )
        assert narration_node_id is not None
        handler = NarrationWorkHandlers(
            session_factory,
            store,  # type: ignore[arg-type]
            generator=narration_generator,
            normalizer=_normalize_narration,
            environment={"ELEVENLABS_TTS_MODEL": "eleven_multilingual_v2"},
        )

        await handler.execute_build_node(narration_node_id)

        assert len(narration_generator.calls) == 1
        assert narration_generator.calls[0].text.endswith("no added sugar.")
        session.expire_all()
        node = await session.get(BuildNode, narration_node_id)
        assert node is not None and node.status == "PASSED"
        attempt = await session.scalar(
            select(Attempt).where(Attempt.build_node_id == narration_node_id)
        )
        assert attempt is not None and attempt.status == "SUCCEEDED"
        assert attempt.genblaze_run_id == "genblaze-run-narration"
        validations = (
            await session.scalars(
                select(Validation).where(Validation.build_node_id == narration_node_id)
            )
        ).all()
        assert {(row.gate_key, row.status) for row in validations} == {
            ("storage_hash", "PASS"),
            ("media_integrity", "PASS"),
            ("audio_properties", "PASS"),
            ("manifest_integrity", "PASS"),
        }
        linked = (
            await session.execute(
                select(AttemptAsset, Asset)
                .join(Asset, Asset.id == AttemptAsset.asset_id)
                .where(AttemptAsset.attempt_id == attempt.id)
            )
        ).all()
        by_role = {link.role: (link, asset) for link, asset in linked}
        assert by_role["provider_raw"][0].selected is False
        assert by_role["narration"][0].selected is True
        assert by_role["narration"][1].mime_type == "audio/wav"
        assert by_role["narration"][1].derived_from_asset_id == by_role["provider_raw"][1].id
        assert by_role["narration"][1].metadata_json["sample_rate"] == 48_000
    finally:
        await _cleanup(session, seeded)


async def test_fetching_narration_recovers_without_second_tts_call(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    narration_generator = FakeNarrationGenerator(store)
    try:
        assert (
            await _runtime(session_factory, store, FakeCopyGenerator()).run_once()
        ).completed == 1
        narration_node_id, _ = await _seed_narration_attempt(
            session, seeded, store, AttemptStatus.FETCHING
        )
        handler = NarrationWorkHandlers(
            session_factory,
            store,  # type: ignore[arg-type]
            generator=narration_generator,
            normalizer=_normalize_narration,
            environment={"ELEVENLABS_TTS_MODEL": "eleven_multilingual_v2"},
        )

        await handler.execute_build_node(narration_node_id)

        assert narration_generator.calls == []
        session.expire_all()
        node = await session.get(BuildNode, narration_node_id)
        assert node is not None and node.status == "PASSED"
    finally:
        await _cleanup(session, seeded)


async def test_submitting_narration_is_not_resubmitted_and_requires_review(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    narration_generator = FakeNarrationGenerator(store)
    try:
        assert (
            await _runtime(session_factory, store, FakeCopyGenerator()).run_once()
        ).completed == 1
        narration_node_id, attempt_id = await _seed_narration_attempt(
            session, seeded, store, AttemptStatus.SUBMITTING
        )
        handler = NarrationWorkHandlers(
            session_factory,
            store,  # type: ignore[arg-type]
            generator=narration_generator,
            normalizer=_normalize_narration,
            environment={"ELEVENLABS_TTS_MODEL": "eleven_multilingual_v2"},
        )

        await handler.execute_build_node(narration_node_id)

        assert narration_generator.calls == []
        session.expire_all()
        attempt = await session.get(Attempt, attempt_id)
        node = await session.get(BuildNode, narration_node_id)
        build = await session.get(Build, seeded.build_id)
        assert attempt is not None and attempt.error_code == "AMBIGUOUS_SUBMISSION"
        assert node is not None and node.status == "WAITING_REVIEW"
        assert build is not None and build.status == "WAITING_REVIEW"
    finally:
        await _cleanup(session, seeded)


async def test_end_card_composes_verified_product_and_copy_inputs(session, session_factory) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    product_sha = hashlib.sha256(PRODUCT_PNG).hexdigest()
    product_key = f"tenants/{seeded.organization_id}/sources/{product_sha}.png"
    store.objects[product_key] = PRODUCT_PNG
    source_node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == "source.product_reference",
        )
    )
    build = await session.get(Build, seeded.build_id)
    assert source_node is not None and build is not None
    source_node.selected_asset_set_hash = product_sha
    asset = Asset(
        id=uuid.uuid4(),
        organization_id=seeded.organization_id,
        sha256=product_sha,
        size_bytes=len(PRODUCT_PNG),
        mime_type="image/png",
        media_kind="IMAGE",
        b2_bucket=store.bucket,
        b2_key=product_key,
        verified_at=datetime.now(UTC),
    )
    source = Source(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        stable_key="source.product_reference",
        kind="IMAGE",
    )
    session.add_all([asset, source])
    await session.flush()
    session.add(
        SourceVersion(
            id=uuid.uuid4(),
            source_id=source.id,
            revision_id=build.project_revision_id,
            asset_id=asset.id,
            content_hash=product_sha,
            created_by=uuid.uuid4(),
        )
    )
    await session.commit()
    try:
        assert (
            await _runtime(session_factory, store, FakeCopyGenerator()).run_once()
        ).completed == 1
        end_card_id = await session.scalar(
            select(BuildNode.id).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "graphic.end_card",
            )
        )
        assert end_card_id is not None

        await EndCardWorkHandlers(
            session_factory,
            store,  # type: ignore[arg-type]
        ).execute_build_node(end_card_id)

        session.expire_all()
        node = await session.get(BuildNode, end_card_id)
        assert node is not None and node.status == "PASSED"
        attempt = await session.scalar(select(Attempt).where(Attempt.build_node_id == end_card_id))
        assert attempt is not None and attempt.provider == "local"
        selected = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(AttemptAsset.attempt_id == attempt.id, AttemptAsset.selected.is_(True))
        )
        assert selected is not None
        assert selected.mime_type == "image/png"
        assert selected.metadata_json["width"] == 1_920
        assert selected.metadata_json["rendered_legal_line"] == "no added sugar"
        validations = (
            await session.scalars(select(Validation).where(Validation.build_node_id == end_card_id))
        ).all()
        assert {(row.gate_key, row.status) for row in validations} == {
            ("required_phrase", "PASS"),
            ("superseded_phrase", "PASS"),
            ("schema", "PASS"),
        }
    finally:
        await _cleanup(session, seeded)


async def _prepare_music_execution(
    session,
    seeded: IncrementalBuild,
    store: MemoryStore,
    *,
    attempt_status: AttemptStatus | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    await session.execute(
        text("delete from work_items where build_id=:build_id"), {"build_id": seeded.build_id}
    )
    build = await session.get(Build, seeded.build_id)
    music_node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == "audio.music",
        )
    )
    plan_node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == "plan.shots",
        )
    )
    assert build is not None and music_node is not None and plan_node is not None
    build.status = str(BuildStatus.RUNNING)
    music_node.status = str(
        BuildNodeStatus.RUNNING if attempt_status is not None else BuildNodeStatus.QUEUED
    )
    music_node.selected_asset_set_hash = None
    music_node.reuse_proof_json = None

    plan_bytes = (
        b'{"shots":[{"index":1,"title":"Emergence","tone":"restrained",'
        b'"camera":"Low-angle medium shot, 85mm equivalent, f/2.8, shallow depth of field"}]}'
    )
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    plan_key = f"tenants/{seeded.organization_id}/plans/{plan_sha}.json"
    store.objects[plan_key] = plan_bytes
    plan_asset = Asset(
        id=uuid.uuid4(),
        organization_id=seeded.organization_id,
        sha256=plan_sha,
        size_bytes=len(plan_bytes),
        mime_type="application/json",
        media_kind="DOCUMENT",
        b2_bucket=store.bucket,
        b2_key=plan_key,
        verified_at=datetime.now(UTC),
    )
    plan_attempt = Attempt(
        id=uuid.uuid4(),
        build_node_id=plan_node.id,
        attempt_no=1,
        mechanism=str(AttemptMechanism.PRIMARY),
        provider="anthropic",
        model="test-plan-model",
        idempotency_key=canonical_hash({"plan_node": str(plan_node.id)}),
        status=str(AttemptStatus.SUCCEEDED),
    )
    session.add_all([plan_asset, plan_attempt])
    await session.flush()
    session.add(
        AttemptAsset(
            id=uuid.uuid4(),
            attempt_id=plan_attempt.id,
            asset_id=plan_asset.id,
            role="plan",
            ordinal=0,
            selected=True,
        )
    )
    plan_node.selected_attempt_id = plan_attempt.id
    plan_node.selected_asset_set_hash = plan_sha

    music_attempt_id: uuid.UUID | None = None
    if attempt_status is not None:
        music_attempt_id = uuid.uuid4()
        music_attempt = Attempt(
            id=music_attempt_id,
            build_node_id=music_node.id,
            attempt_no=1,
            mechanism=str(AttemptMechanism.PRIMARY),
            provider="elevenlabs",
            model="music_v2",
            idempotency_key=submission_idempotency_key(
                build_node_id=music_node.id,
                fingerprint=music_node.fingerprint,
                mechanism=AttemptMechanism.PRIMARY,
                provider="elevenlabs",
                model="music_v2",
            ),
            status=str(attempt_status),
        )
        session.add(music_attempt)
        if attempt_status is AttemptStatus.FETCHING:
            result = _music_result(store, seeded.organization_id)
            session.add(
                AttemptEvent(
                    attempt_id=music_attempt_id,
                    provider_event_type="attempt.fetching",
                    provider_event_json=result.model_dump(mode="json"),
                )
            )
    await session.flush()
    await WorkQueue(session).enqueue(
        kind="EXECUTE_BUILD_NODE",
        target_id=music_node.id,
        build_id=build.id,
        priority=60,
        dedupe_key=f"execute:music:{music_node.id}",
        payload={"stable_key": "audio.music", "trigger_source": "APPLICATION_COMMIT"},
    )
    await session.commit()
    return music_node.id, music_attempt_id


def _music_runtime(
    session_factory,
    store: MemoryStore,
    generator: FakeMusicGenerator,
) -> WorkerRuntime:
    music_handlers = MusicWorkHandlers(
        session_factory,
        store,  # type: ignore[arg-type]
        generator=generator,
        prober=_probe_music,
        environment={"ELEVENLABS_MUSIC_MODEL": "music_v2"},
    )
    return WorkerRuntime(
        session_factory,
        store,  # type: ignore[arg-type]
        owner="music-worker-test",
        lease_seconds=30,
        heartbeat_seconds=5,
        concurrency=1,
        music_handlers=music_handlers,
    )


async def test_music_worker_persists_verified_asset_and_validation_evidence(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakeMusicGenerator(store)
    try:
        music_node_id, _ = await _prepare_music_execution(session, seeded, store)
        music_node = await session.get(BuildNode, music_node_id)
        assert music_node is not None
        persisted_graph_node = await session.get(GraphNode, music_node.graph_node_id)
        assert persisted_graph_node is not None
        assert isinstance(persisted_graph_node.spec_json.get("normalized_operation"), dict), (
            persisted_graph_node.spec_json
        )

        receipt = await _music_runtime(session_factory, store, generator).run_once()

        worker_error = await session.scalar(
            select(WorkItem.last_error).where(WorkItem.target_id == music_node_id)
        )
        assert receipt.completed == 1, worker_error
        assert len(generator.calls) == 1
        assert generator.calls[0].duration_ms == 20_000
        prompt = generator.calls[0].prompt
        assert "Dark graphite set" in prompt
        # Pacing reaches the music model as shot beats.
        assert "Emergence" in prompt
        assert "Total length: 20 seconds." in prompt
        # The shot plan is written for a video model. Pasting it in whole is what
        # pushed the prompt past ElevenLabs' 4,100-character limit and failed the
        # node on a 422 that no retry could clear, so the camera direction must
        # not be carried across.
        assert "85mm equivalent" not in prompt
        assert '"tone":"restrained"' not in prompt
        assert len(prompt) <= MUSIC_PROMPT_LIMIT
        session.expire_all()
        node = await session.get(BuildNode, music_node_id)
        assert node is not None and node.status == "PASSED"
        attempt = await session.scalar(
            select(Attempt).where(Attempt.build_node_id == music_node_id)
        )
        assert attempt is not None and attempt.status == "SUCCEEDED"
        selected = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(AttemptAsset.attempt_id == attempt.id, AttemptAsset.selected.is_(True))
        )
        assert selected is not None and selected.mime_type == "audio/mpeg"
        assert selected.metadata_json["duration_ms"] == 20_000
        assert selected.metadata_json["sample_rate"] == 48_000
        validations = (
            await session.scalars(
                select(Validation).where(Validation.build_node_id == music_node_id)
            )
        ).all()
        assert {(row.gate_key, row.status) for row in validations} == {
            ("storage_hash", "PASS"),
            ("media_integrity", "PASS"),
            ("audio_properties", "PASS"),
        }
    finally:
        await _cleanup(session, seeded)


async def test_fetching_music_recovers_without_second_provider_call(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakeMusicGenerator(store)
    try:
        music_node_id, _ = await _prepare_music_execution(
            session, seeded, store, attempt_status=AttemptStatus.FETCHING
        )

        receipt = await _music_runtime(session_factory, store, generator).run_once()

        assert receipt.completed == 1
        assert generator.calls == []
        session.expire_all()
        node = await session.get(BuildNode, music_node_id)
        assert node is not None and node.status == "PASSED"
    finally:
        await _cleanup(session, seeded)


async def test_music_storage_verification_retries_without_second_provider_call(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = FlakyMusicStore()
    generator = FakeMusicGenerator(store)
    try:
        music_node_id, _ = await _prepare_music_execution(session, seeded, store)
        runtime = _music_runtime(session_factory, store, generator)

        first = await runtime.run_once()

        assert first.failed == 1
        assert len(generator.calls) == 1
        attempt = await session.scalar(
            select(Attempt).where(Attempt.build_node_id == music_node_id)
        )
        assert attempt is not None and attempt.status == "FETCHING"
        await session.execute(
            update(WorkItem)
            .where(WorkItem.target_id == music_node_id)
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

        second = await runtime.run_once()

        assert second.completed == 1
        assert len(generator.calls) == 1
        session.expire_all()
        node = await session.get(BuildNode, music_node_id)
        assert node is not None and node.status == "PASSED"
    finally:
        await _cleanup(session, seeded)


async def test_submitting_music_is_not_resubmitted_and_requires_review(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakeMusicGenerator(store)
    try:
        music_node_id, attempt_id = await _prepare_music_execution(
            session, seeded, store, attempt_status=AttemptStatus.SUBMITTING
        )
        assert attempt_id is not None

        receipt = await _music_runtime(session_factory, store, generator).run_once()

        assert receipt.completed == 1
        assert generator.calls == []
        session.expire_all()
        attempt = await session.get(Attempt, attempt_id)
        node = await session.get(BuildNode, music_node_id)
        build = await session.get(Build, seeded.build_id)
        assert attempt is not None and attempt.error_code == "AMBIGUOUS_SUBMISSION"
        assert attempt.status == "FAILED"
        assert node is not None and node.status == "WAITING_REVIEW"
        assert build is not None and build.status == "WAITING_REVIEW"
    finally:
        await _cleanup(session, seeded)


async def _prepare_plan_execution(
    session,
    seeded: IncrementalBuild,
    store: MemoryStore,
    *,
    attempt_status: AttemptStatus | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    await session.execute(
        text("delete from work_items where build_id=:build_id"), {"build_id": seeded.build_id}
    )
    build = await session.get(Build, seeded.build_id)
    plan_node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == "plan.shots",
        )
    )
    product_node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == "source.product_reference",
        )
    )
    assert build is not None and plan_node is not None and product_node is not None
    build.status = str(BuildStatus.RUNNING)
    plan_node.status = str(
        BuildNodeStatus.RUNNING if attempt_status is not None else BuildNodeStatus.QUEUED
    )
    plan_node.selected_attempt_id = None
    plan_node.selected_asset_set_hash = None
    plan_node.reuse_proof_json = None

    product_sha = hashlib.sha256(PRODUCT_PNG).hexdigest()
    product_key = f"tenants/{seeded.organization_id}/sources/{product_sha}.png"
    store.objects[product_key] = PRODUCT_PNG
    product_asset = Asset(
        id=uuid.uuid4(),
        organization_id=seeded.organization_id,
        sha256=product_sha,
        size_bytes=len(PRODUCT_PNG),
        mime_type="image/png",
        media_kind="IMAGE",
        b2_bucket=store.bucket,
        b2_key=product_key,
        verified_at=datetime.now(UTC),
    )
    product_source = Source(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        stable_key="source.product_reference",
        kind="IMAGE",
    )
    session.add_all([product_asset, product_source])
    await session.flush()
    session.add(
        SourceVersion(
            id=uuid.uuid4(),
            source_id=product_source.id,
            revision_id=build.project_revision_id,
            asset_id=product_asset.id,
            content_hash=product_sha,
            created_by=uuid.uuid4(),
        )
    )
    product_node.selected_asset_set_hash = product_sha
    await session.execute(
        update(BuildNode)
        .where(
            BuildNode.build_id == build.id,
            BuildNode.stable_key.in_({"copy.pack", "audio.narration", "graphic.end_card"}),
        )
        .values(status=str(BuildNodeStatus.REUSED))
    )

    schedulable = {
        "audio.music",
        "image.keyframe.01",
        "image.keyframe.02",
        "image.keyframe.03",
        "image.keyframe.04",
    }
    await session.execute(
        update(BuildNode)
        .where(
            BuildNode.build_id == build.id,
            BuildNode.stable_key.in_(schedulable),
        )
        .values(status=str(BuildNodeStatus.PENDING), selected_asset_set_hash=None)
    )

    plan_attempt_id: uuid.UUID | None = None
    if attempt_status is not None:
        plan_attempt_id = uuid.uuid4()
        session.add(
            Attempt(
                id=plan_attempt_id,
                build_node_id=plan_node.id,
                attempt_no=1,
                mechanism=str(AttemptMechanism.PRIMARY),
                provider="anthropic",
                model="test-plan-model",
                idempotency_key=submission_idempotency_key(
                    build_node_id=plan_node.id,
                    fingerprint=plan_node.fingerprint,
                    mechanism=AttemptMechanism.PRIMARY,
                    provider="anthropic",
                    model="test-plan-model",
                ),
                status=str(attempt_status),
            )
        )
        if attempt_status is AttemptStatus.FETCHING:
            session.add(
                AttemptEvent(
                    attempt_id=plan_attempt_id,
                    provider_event_type="attempt.fetching",
                    provider_event_json=_plan_result().model_dump(mode="json"),
                )
            )
    await session.flush()
    await WorkQueue(session).enqueue(
        kind="EXECUTE_BUILD_NODE",
        target_id=plan_node.id,
        build_id=build.id,
        priority=80,
        dedupe_key=f"execute:plan:{plan_node.id}",
        payload={"stable_key": "plan.shots", "trigger_source": "APPLICATION_COMMIT"},
    )
    await session.commit()
    return plan_node.id, plan_attempt_id


def _plan_runtime(
    session_factory,
    store: MemoryStore,
    generator: FakePlanGenerator,
) -> WorkerRuntime:
    handlers = PlanWorkHandlers(
        session_factory,
        store,  # type: ignore[arg-type]
        generator=generator,
        environment={"EVALUATOR_MODEL": "test-plan-model"},
    )
    return WorkerRuntime(
        session_factory,
        store,  # type: ignore[arg-type]
        owner="plan-worker-test",
        lease_seconds=30,
        heartbeat_seconds=5,
        concurrency=1,
        plan_handlers=handlers,
    )


async def test_plan_worker_uses_verified_image_and_schedules_five_children(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakePlanGenerator()
    try:
        plan_node_id, _ = await _prepare_plan_execution(session, seeded, store)

        receipt = await _plan_runtime(session_factory, store, generator).run_once()

        worker_error = await session.scalar(
            select(WorkItem.last_error).where(WorkItem.target_id == plan_node_id)
        )
        assert receipt.completed == 1, worker_error
        assert len(generator.calls) == 1
        assert generator.calls[0].product_reference_bytes == PRODUCT_PNG
        assert (
            generator.calls[0].product_reference_sha256 == hashlib.sha256(PRODUCT_PNG).hexdigest()
        )
        assert "Dark graphite set" in generator.calls[0].brief
        session.expire_all()
        node = await session.get(BuildNode, plan_node_id)
        assert node is not None and node.status == "PASSED"
        attempt = await session.scalar(select(Attempt).where(Attempt.build_node_id == plan_node_id))
        assert attempt is not None and attempt.status == "SUCCEEDED"
        selected = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(AttemptAsset.attempt_id == attempt.id, AttemptAsset.selected.is_(True))
        )
        assert selected is not None and selected.metadata_json["schema"] == "shot_plan.v1"
        stored_plan = ShotPlan.model_validate_json(store.objects[selected.b2_key])
        assert [shot.index for shot in stored_plan.shots] == [1, 2, 3, 4]
        validations = (
            await session.scalars(
                select(Validation).where(Validation.build_node_id == plan_node_id)
            )
        ).all()
        assert {(row.gate_key, row.status) for row in validations} == {
            ("schema", "PASS"),
            ("shot_count", "PASS"),
            ("duration", "PASS"),
            ("storage_hash", "PASS"),
        }
        queued = set(
            await session.scalars(
                select(BuildNode.stable_key).where(
                    BuildNode.build_id == seeded.build_id,
                    BuildNode.status == str(BuildNodeStatus.QUEUED),
                )
            )
        )
        assert queued == {
            "audio.music",
            "image.keyframe.01",
            "image.keyframe.02",
            "image.keyframe.03",
            "image.keyframe.04",
        }
    finally:
        await _cleanup(session, seeded)


async def test_fetching_plan_recovers_without_second_anthropic_call(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakePlanGenerator()
    try:
        plan_node_id, _ = await _prepare_plan_execution(
            session, seeded, store, attempt_status=AttemptStatus.FETCHING
        )

        receipt = await _plan_runtime(session_factory, store, generator).run_once()

        assert receipt.completed == 1
        assert generator.calls == []
        session.expire_all()
        node = await session.get(BuildNode, plan_node_id)
        assert node is not None and node.status == "PASSED"
    finally:
        await _cleanup(session, seeded)


async def test_submitting_plan_is_not_resubmitted_and_requires_review(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    generator = FakePlanGenerator()
    try:
        plan_node_id, attempt_id = await _prepare_plan_execution(
            session, seeded, store, attempt_status=AttemptStatus.SUBMITTING
        )
        assert attempt_id is not None

        receipt = await _plan_runtime(session_factory, store, generator).run_once()

        assert receipt.completed == 1
        assert generator.calls == []
        session.expire_all()
        attempt = await session.get(Attempt, attempt_id)
        node = await session.get(BuildNode, plan_node_id)
        build = await session.get(Build, seeded.build_id)
        assert attempt is not None and attempt.error_code == "AMBIGUOUS_SUBMISSION"
        assert node is not None and node.status == "WAITING_REVIEW"
        assert build is not None and build.status == "WAITING_REVIEW"
    finally:
        await _cleanup(session, seeded)


def _local_image_runtime(session_factory, store: MemoryStore) -> WorkerRuntime:
    handlers = LocalImageWorkHandlers(
        session_factory,
        store,  # type: ignore[arg-type]
    )
    return WorkerRuntime(
        session_factory,
        store,  # type: ignore[arg-type]
        owner="local-image-worker-test",
        lease_seconds=30,
        heartbeat_seconds=5,
        concurrency=1,
        local_image_handlers=handlers,
    )


async def test_cutout_worker_stores_transparent_png_and_schedules_keyframes(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    try:
        await _prepare_plan_execution(session, seeded, store)
        await session.execute(
            text("delete from work_items where build_id=:build_id"),
            {"build_id": seeded.build_id},
        )
        cutout = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "transform.product_cutout",
            )
        )
        assert cutout is not None
        cutout_id = cutout.id
        cutout.status = str(BuildNodeStatus.QUEUED)
        await session.execute(
            update(BuildNode)
            .where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "plan.shots",
            )
            .values(status=str(BuildNodeStatus.REUSED))
        )
        await WorkQueue(session).enqueue(
            kind="EXECUTE_BUILD_NODE",
            target_id=cutout.id,
            build_id=seeded.build_id,
            priority=70,
            dedupe_key=f"execute:cutout:{cutout.id}",
        )
        await session.commit()

        receipt = await _local_image_runtime(session_factory, store).run_once()

        assert receipt.completed == 1
        session.expire_all()
        node = await session.get(BuildNode, cutout_id)
        assert node is not None and node.status == "PASSED"
        selected = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .join(Attempt, Attempt.id == AttemptAsset.attempt_id)
            .where(Attempt.build_node_id == cutout_id, AttemptAsset.selected.is_(True))
        )
        assert selected is not None and selected.metadata_json["has_alpha"] is True
        with Image.open(io.BytesIO(store.objects[selected.b2_key])) as image:
            assert image.getchannel("A").getpixel((0, 0)) == 0
            assert image.getchannel("A").getpixel((120, 160)) > 200
        queued_keyframes = set(
            await session.scalars(
                select(BuildNode.stable_key).where(
                    BuildNode.build_id == seeded.build_id,
                    BuildNode.status == str(BuildNodeStatus.QUEUED),
                    BuildNode.stable_key.like("image.keyframe.%"),
                )
            )
        )
        assert queued_keyframes == {
            "image.keyframe.01",
            "image.keyframe.02",
            "image.keyframe.03",
            "image.keyframe.04",
        }
    finally:
        await _cleanup(session, seeded)


async def test_poster_worker_combines_selected_product_and_keyframe(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    try:
        await _prepare_plan_execution(session, seeded, store)
        await session.execute(
            text("delete from work_items where build_id=:build_id"),
            {"build_id": seeded.build_id},
        )
        keyframe = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "image.keyframe.01",
            )
        )
        poster = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "image.poster",
            )
        )
        assert keyframe is not None and poster is not None
        poster_id = poster.id
        frame_sha = hashlib.sha256(PNG_1X1).hexdigest()
        frame_key = f"tenants/{seeded.organization_id}/frames/{frame_sha}.png"
        store.objects[frame_key] = PNG_1X1
        frame_asset = Asset(
            id=uuid.uuid4(),
            organization_id=seeded.organization_id,
            sha256=frame_sha,
            size_bytes=len(PNG_1X1),
            mime_type="image/png",
            media_kind="IMAGE",
            b2_bucket=store.bucket,
            b2_key=frame_key,
            verified_at=datetime.now(UTC),
        )
        frame_attempt = Attempt(
            id=uuid.uuid4(),
            build_node_id=keyframe.id,
            attempt_no=1,
            mechanism=str(AttemptMechanism.PRIMARY),
            provider="gmicloud",
            model="image-model",
            idempotency_key=canonical_hash({"poster-frame": str(keyframe.id)}),
            status=str(AttemptStatus.SUCCEEDED),
        )
        session.add_all([frame_asset, frame_attempt])
        await session.flush()
        session.add(
            AttemptAsset(
                id=uuid.uuid4(),
                attempt_id=frame_attempt.id,
                asset_id=frame_asset.id,
                role="primary",
                ordinal=0,
                selected=True,
            )
        )
        keyframe.status = str(BuildNodeStatus.PASSED)
        keyframe.selected_attempt_id = frame_attempt.id
        keyframe.selected_asset_set_hash = frame_sha
        poster.status = str(BuildNodeStatus.QUEUED)
        await WorkQueue(session).enqueue(
            kind="EXECUTE_BUILD_NODE",
            target_id=poster.id,
            build_id=seeded.build_id,
            priority=40,
            dedupe_key=f"execute:poster:{poster.id}",
        )
        await session.commit()

        receipt = await _local_image_runtime(session_factory, store).run_once()

        assert receipt.completed == 1
        session.expire_all()
        node = await session.get(BuildNode, poster_id)
        assert node is not None and node.status == "PASSED"
        selected = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .join(Attempt, Attempt.id == AttemptAsset.attempt_id)
            .where(Attempt.build_node_id == poster_id, AttemptAsset.selected.is_(True))
        )
        assert selected is not None
        assert selected.metadata_json["width"] == 1_080
        assert selected.metadata_json["height"] == 1_350
    finally:
        await _cleanup(session, seeded)


def _gmi_runtime(
    session_factory,
    store: MemoryStore,
    gateway: FakeGMIGateway,
) -> WorkerRuntime:
    handlers = GMIWorkHandlers(
        session_factory,
        store,  # type: ignore[arg-type]
        gateway,
        environment={
            "GMI_IMAGE_MODEL": "test-image-model",
            "GMI_VIDEO_MODEL": "test-video-model",
            "GMI_VIDEO_FALLBACK_MODEL": "test-video-fallback",
        },
    )
    return WorkerRuntime(
        session_factory,
        store,  # type: ignore[arg-type]
        owner="gmi-worker-test",
        lease_seconds=30,
        heartbeat_seconds=5,
        concurrency=1,
        gmi_handlers=handlers,
    )


async def test_keyframe_and_clip_workers_use_gmi_durable_outputs(
    session, session_factory, tmp_path
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    plan_generator = FakePlanGenerator()
    gateway = FakeGMIGateway(store)
    try:
        plan_node_id, _ = await _prepare_plan_execution(session, seeded, store)
        plan_receipt = await _plan_runtime(session_factory, store, plan_generator).run_once()
        assert plan_receipt.completed == 1
        await session.execute(
            text("delete from work_items where build_id=:build_id"),
            {"build_id": seeded.build_id},
        )
        cutout = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "transform.product_cutout",
            )
        )
        plan_node = await session.get(BuildNode, plan_node_id)
        assert cutout is not None and plan_node is not None
        cutout.status = str(BuildNodeStatus.QUEUED)
        await WorkQueue(session).enqueue(
            kind="EXECUTE_BUILD_NODE",
            target_id=cutout.id,
            build_id=seeded.build_id,
            priority=70,
            dedupe_key=f"execute:cutout:gmi:{cutout.id}",
        )
        await session.commit()
        cutout_receipt = await _local_image_runtime(session_factory, store).run_once()
        assert cutout_receipt.completed == 1

        session.expire_all()
        keyframe = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "image.keyframe.01",
            )
        )
        assert keyframe is not None and keyframe.status == "QUEUED"
        keyframe_id = keyframe.id
        await session.execute(
            update(BuildNode)
            .where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "video.clip.01",
            )
            .values(status=str(BuildNodeStatus.PENDING))
        )
        await session.execute(
            text("delete from work_items where build_id=:build_id"),
            {"build_id": seeded.build_id},
        )
        await WorkQueue(session).enqueue(
            kind="EXECUTE_BUILD_NODE",
            target_id=keyframe_id,
            build_id=seeded.build_id,
            priority=60,
            dedupe_key=f"execute:keyframe:test:{keyframe_id}",
        )
        await session.commit()

        receipt = await _gmi_runtime(session_factory, store, gateway).run_once()

        assert receipt.completed == 1
        assert len(gateway.calls) == 1
        request = gateway.calls[0]
        assert request.stable_key == "image.keyframe.01"
        assert request.model == "test-image-model"
        assert len(request.inputs) == 2
        assert "shot 1" in request.prompt.casefold()
        session.expire_all()
        completed = await session.get(BuildNode, keyframe_id)
        assert completed is not None and completed.status == "PASSED"
        attempt = await session.scalar(select(Attempt).where(Attempt.build_node_id == keyframe_id))
        assert attempt is not None
        assert attempt.status == "SUCCEEDED"
        assert attempt.genblaze_run_id == "run_test_gmi"
        clip_status = await session.scalar(
            select(BuildNode.status).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "video.clip.01",
            )
        )
        assert clip_status == "QUEUED"
        clip_path = tmp_path / "generated.mp4"
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#111820:s=320x180:r=30:d=0.25",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip_path),
        ]
        await asyncio.to_thread(
            subprocess.run,  # noqa: S603
            command,
            check=True,
            capture_output=True,
        )
        gateway.output = clip_path.read_bytes()

        clip_receipt = await _gmi_runtime(session_factory, store, gateway).run_once()

        assert clip_receipt.completed == 1
        assert len(gateway.calls) == 2
        assert gateway.calls[1].stable_key == "video.clip.01"
        assert gateway.calls[1].model == "test-video-model"
        assert gateway.calls[1].fallback_models == ("test-video-fallback",)
        assert len(gateway.calls[1].inputs) == 1
        session.expire_all()
        completed_clip = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "video.clip.01",
            )
        )
        assert completed_clip is not None and completed_clip.status == "PASSED"
    finally:
        await _cleanup(session, seeded)


async def _attach_selected_output(
    session,
    store: MemoryStore,
    seeded: IncrementalBuild,
    stable_key: str,
    data: bytes,
    mime_type: str,
    media_kind: str,
) -> None:
    node = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == seeded.build_id,
            BuildNode.stable_key == stable_key,
        )
    )
    assert node is not None
    digest = hashlib.sha256(data).hexdigest()
    extension = mime_type.rsplit("/", 1)[-1].replace("mpeg", "mp3")
    key = f"tenants/{seeded.organization_id}/delivery-inputs/{digest}.{extension}"
    store.objects[key] = data
    asset = Asset(
        id=uuid.uuid4(),
        organization_id=seeded.organization_id,
        sha256=digest,
        size_bytes=len(data),
        mime_type=mime_type,
        media_kind=media_kind,
        b2_bucket=store.bucket,
        b2_key=key,
        verified_at=datetime.now(UTC),
    )
    attempt = Attempt(
        id=uuid.uuid4(),
        build_node_id=node.id,
        attempt_no=99,
        mechanism=str(AttemptMechanism.PRIMARY),
        provider="test",
        model="test-output",
        idempotency_key=canonical_hash({"delivery-input": stable_key, "sha256": digest}),
        status=str(AttemptStatus.SUCCEEDED),
    )
    session.add_all([asset, attempt])
    await session.flush()
    session.add(
        AttemptAsset(
            id=uuid.uuid4(),
            attempt_id=attempt.id,
            asset_id=asset.id,
            role="selected",
            ordinal=0,
            selected=True,
        )
    )
    node.status = str(BuildNodeStatus.REUSED)
    node.selected_attempt_id = attempt.id
    node.selected_asset_set_hash = digest


async def test_delivery_worker_stores_seven_assets_and_completes_build(
    session, session_factory
) -> None:
    seeded = await _seed_incremental_build(session)
    store = MemoryStore()
    composer = FakeDeliveryComposer()
    try:
        build = await session.get(Build, seeded.build_id)
        delivery = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == seeded.build_id,
                BuildNode.stable_key == "compose.delivery_package",
            )
        )
        assert build is not None and delivery is not None
        build.status = str(BuildStatus.RUNNING)
        await session.execute(
            update(BuildNode)
            .where(BuildNode.build_id == seeded.build_id, BuildNode.id != delivery.id)
            .values(status=str(BuildNodeStatus.REUSED))
        )
        inputs = {
            "video.clip.01": (b"clip-one", "video/mp4", "VIDEO"),
            "video.clip.02": (b"clip-two", "video/mp4", "VIDEO"),
            "video.clip.03": (b"clip-three", "video/mp4", "VIDEO"),
            "video.clip.04": (b"clip-four", "video/mp4", "VIDEO"),
            "audio.narration": (b"narration", "audio/wav", "AUDIO"),
            "audio.music": (b"music", "audio/mpeg", "AUDIO"),
            "graphic.end_card": (b"end-card", "image/png", "IMAGE"),
            "copy.pack": (
                _provider_result(required_line="no added sugar").output.model_dump_json().encode(),
                "application/json",
                "DOCUMENT",
            ),
        }
        for stable_key, (data, mime_type, media_kind) in inputs.items():
            await _attach_selected_output(
                session,
                store,
                seeded,
                stable_key,
                data,
                mime_type,
                media_kind,
            )
        delivery.status = str(BuildNodeStatus.QUEUED)
        delivery_id = delivery.id
        await session.execute(
            text("delete from work_items where build_id=:build_id"),
            {"build_id": seeded.build_id},
        )
        await WorkQueue(session).enqueue(
            kind="EXECUTE_BUILD_NODE",
            target_id=delivery_id,
            build_id=seeded.build_id,
            priority=10,
            dedupe_key=f"execute:delivery:test:{delivery_id}",
        )
        await session.commit()
        handlers = DeliveryWorkHandlers(
            session_factory,
            store,  # type: ignore[arg-type]
            composer=composer,
        )
        runtime = WorkerRuntime(
            session_factory,
            store,  # type: ignore[arg-type]
            owner="delivery-worker-test",
            lease_seconds=30,
            heartbeat_seconds=5,
            concurrency=1,
            delivery_handlers=handlers,
        )

        receipt = await runtime.run_once()

        assert receipt.completed == 1
        assert len(composer.calls) == 1
        assert composer.calls[0].clips == (
            b"clip-one",
            b"clip-two",
            b"clip-three",
            b"clip-four",
        )
        session.expire_all()
        completed_node = await session.get(BuildNode, delivery_id)
        completed_build = await session.get(Build, seeded.build_id)
        assert completed_node is not None and completed_node.status == "PASSED"
        assert completed_build is not None and completed_build.status == "SUCCEEDED"
        attempt = await session.scalar(select(Attempt).where(Attempt.build_node_id == delivery_id))
        assert attempt is not None and attempt.status == "SUCCEEDED"
        roles = set(
            await session.scalars(
                select(AttemptAsset.role).where(AttemptAsset.attempt_id == attempt.id)
            )
        )
        assert roles == {
            "master_16x9",
            "master_9x16",
            "final_audio",
            "thumbnail_16x9",
            "thumbnail_9x16",
            "captions",
            "report",
        }
    finally:
        await _cleanup(session, seeded)


def test_music_prompt_stays_within_the_provider_limit_for_a_rich_shot_plan() -> None:
    """A four-shot plan with production-grade detail must still fit.

    The plan that broke the live build composed to 4,551 characters against a
    provider limit of 4,100. The failure was terminal — ElevenLabs answers 422,
    which no retry policy will ever clear — so the whole build died with it.
    """
    plan = {
        "schema_version": "shot_plan.v1",
        "shots": [
            {
                "index": index,
                "title": f"Shot {index}",
                "camera": "Low-angle medium shot, 85mm equivalent, f/2.8. " + "x" * 400,
                "motion": "Slow vertical rise with ease-in and ease-out. " + "y" * 400,
                "visual_direction": "Deep graphite void, matte white bottle. " + "z" * 600,
                "duration_seconds": 4,
            }
            for index in range(1, 5)
        ],
    }
    prompt = _compose_prompt(
        "Compose a restrained cinematic bed matching the brief tone.",
        "ORBIT Hydration launch. Dark graphite set, crisp white bottle.",
        plan,
        20,
    )
    assert len(prompt) <= MUSIC_PROMPT_LIMIT
    assert "Shot 1" in prompt and "Shot 4" in prompt
    assert "85mm equivalent" not in prompt


def test_music_prompt_clamps_an_oversized_brief() -> None:
    """A long brief must not push the prompt past the provider limit."""
    prompt = _compose_prompt(
        "Compose a restrained cinematic bed.",
        "ORBIT. " + "long brief text. " * 500,
        {"shots": [{"index": 1, "title": "Emergence"}]},
        20,
    )
    assert len(prompt) <= MUSIC_PROMPT_LIMIT
    assert "Emergence" in prompt
