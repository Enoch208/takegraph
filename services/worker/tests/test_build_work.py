"""Crash boundaries for incremental build-node execution on real PostgreSQL."""

from __future__ import annotations

import base64
import copy
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

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
from takegraph_worker.end_card_work import EndCardWorkHandlers
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
