"""Crash boundaries for incremental build-node execution on real PostgreSQL."""

from __future__ import annotations

import base64
import copy
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    DEFAULT_LEGAL_LINE,
    EXPECTED_LEGAL_COPY_REBUILD,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
)
from takegraph_infrastructure.b2 import StoredObject
from takegraph_infrastructure.media import MediaProbe
from takegraph_worker.anthropic_gateway import (
    CopyGenerationRequest,
    CopyGenerationResult,
    CopyPack,
)
from takegraph_worker.build_work import BuildWorkHandlers
from takegraph_worker.elevenlabs_gateway import NarrationRequest, NarrationResult
from takegraph_worker.elevenlabs_music_gateway import (
    MusicGenerationRequest,
    MusicGenerationResult,
)
from takegraph_worker.end_card_work import EndCardWorkHandlers
from takegraph_worker.music_work import MusicWorkHandlers
from takegraph_worker.narration_work import NarrationWorkHandlers
from takegraph_worker.runtime import WorkerRuntime

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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

    def key_from_url(self, url: str) -> str | None:
        prefix = "https://memory.invalid/"
        return url[len(prefix) :] if url.startswith(prefix) else None


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
    product_sha = hashlib.sha256(PNG_1X1).hexdigest()
    product_key = f"tenants/{seeded.organization_id}/sources/{product_sha}.png"
    store.objects[product_key] = PNG_1X1
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
        size_bytes=len(PNG_1X1),
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

    plan_bytes = b'{"shots":[{"index":1,"tone":"restrained"}]}'
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
        assert "Dark graphite set" in generator.calls[0].prompt
        assert '"tone":"restrained"' in generator.calls[0].prompt
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
