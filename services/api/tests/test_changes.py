"""Change-set, impact-plan, and atomic incremental-build integration tests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_api.changes import (
    GENERATOR_CODE_VERSION,
    ChangeImpactService,
    ChangeSetCreateRequest,
    ChangeSetResponse,
    ChangeSetUpdateRequest,
    ImpactCommitRequest,
    ImpactPlanResponse,
    RevisionParametersPatch,
    RevisionPatch,
)
from takegraph_api.db.models import (
    Asset,
    Attempt,
    AttemptAsset,
    Build,
    BuildNode,
    ChangeSet,
    GraphNode,
    GraphRevision,
    ImpactPlanRow,
    Organization,
    Project,
    ProjectRevision,
    Source,
    SourceVersion,
    WorkItem,
)
from takegraph_api.projects import ProjectCreateRequest, ProjectResponse, ProjectService
from takegraph_domain.auth import Principal
from takegraph_domain.canonical import canonical_hash
from takegraph_domain.enums import BuildNodeStatus, Role
from takegraph_domain.errors import IdempotencyConflictError, ImpactPlanStaleError
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.fingerprint import compute_fingerprint, compute_source_fingerprint
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    DEFAULT_LEGAL_LINE,
    EXPECTED_LEGAL_COPY_REBUILD,
    ORBIT_TEMPLATE,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
)
from takegraph_domain.graph.orbit_policies import orbit_policy_hashes

COMPLETE_ENV = {
    "GMI_API_KEY": "configured",
    "GMI_IMAGE_MODEL": "image-model",
    "GMI_VIDEO_MODEL": "video-model",
    "GMI_VIDEO_FALLBACK_MODEL": "video-fallback",
    "ELEVENLABS_API_KEY": "configured",
    "ELEVENLABS_MUSIC_MODEL": "music-model",
    "ELEVENLABS_TTS_MODEL": "tts-model",
    "ELEVENLABS_VOICE_ID": "voice-id",
    "ANTHROPIC_API_KEY": "configured",
    "EVALUATOR_MODEL": "text-model",
}
PRODUCT_HASH = "a1" * 32


@pytest.fixture
def owner() -> Principal:
    return Principal(
        actor_id=uuid.uuid4(),
        subject="change-test-owner",
        organization_id=uuid.uuid4(),
        role=Role.OWNER,
    )


def legal_patch(value: str) -> RevisionPatch:
    return RevisionPatch(parameters=RevisionParametersPatch(legal_line=value))


async def seed_completed_orbit(
    session: AsyncSession, owner: Principal
) -> tuple[ProjectResponse, ProjectRevision, GraphRevision, Build]:
    session.add(
        Organization(
            id=owner.organization_id,
            slug=f"changes-{uuid.uuid4().hex}",
            name="Change Tests",
        )
    )
    await session.flush()
    project = await ProjectService(session).create(
        principal=owner,
        request=ProjectCreateRequest(
            slug=f"orbit-{uuid.uuid4().hex[:12]}",
            name="ORBIT",
            spec={
                "parameters": {
                    PARAM_LEGAL_LINE: DEFAULT_LEGAL_LINE,
                    PARAM_BRIEF_TEXT: DEFAULT_BRIEF_TEXT,
                }
            },
        ),
    )
    revision = await session.get(ProjectRevision, project.active_revision_id)
    assert revision is not None
    graph_row = await session.scalar(
        select(GraphRevision).where(GraphRevision.project_revision_id == revision.id)
    )
    assert graph_row is not None
    graph = compile_graph(
        ORBIT_TEMPLATE,
        parameters={
            PARAM_LEGAL_LINE: DEFAULT_LEGAL_LINE,
            PARAM_BRIEF_TEXT: DEFAULT_BRIEF_TEXT,
        },
        policy_hashes=orbit_policy_hashes(),
    )
    graph_nodes = (
        await session.scalars(select(GraphNode).where(GraphNode.graph_revision_id == graph_row.id))
    ).all()
    graph_nodes_by_key = {node.stable_key: node for node in graph_nodes}

    product_asset = Asset(
        id=uuid.uuid4(),
        organization_id=owner.organization_id,
        sha256=PRODUCT_HASH,
        size_bytes=1024,
        mime_type="image/png",
        media_kind="IMAGE",
        b2_bucket="test-work",
        b2_key=f"test/{PRODUCT_HASH}.png",
        verified_at=datetime.now(UTC),
    )
    product_source = Source(
        id=uuid.uuid4(),
        project_id=project.id,
        stable_key="source.product_reference",
        kind="IMAGE",
    )
    session.add_all([product_asset, product_source])
    await session.flush()
    session.add(
        SourceVersion(
            id=uuid.uuid4(),
            source_id=product_source.id,
            revision_id=revision.id,
            asset_id=product_asset.id,
            content_hash=PRODUCT_HASH,
            created_by=owner.actor_id,
        )
    )

    build = Build(
        id=uuid.uuid4(),
        project_id=project.id,
        project_revision_id=revision.id,
        graph_revision_id=graph_row.id,
        status="SUCCEEDED",
        total_nodes=18,
        rebuilt_nodes=18,
        reused_nodes=0,
        is_fixture=False,
        completed_at=datetime.now(UTC),
        version=1,
    )
    session.add(build)
    await session.flush()

    source_hashes = {
        "source.brief": hashlib.sha256(DEFAULT_BRIEF_TEXT.encode()).hexdigest(),
        "source.product_reference": PRODUCT_HASH,
    }
    output_refs: dict[str, str | None] = {}
    for stable_key in graph.topological_order:
        compiled_node = graph.by_key[stable_key]
        graph_node = graph_nodes_by_key[stable_key]
        if compiled_node.node_type.is_source:
            selected_hash = source_hashes[stable_key]
            fingerprint = compute_source_fingerprint(compiled_node, content_hash=selected_hash)
        else:
            fingerprint = compute_fingerprint(
                compiled_node,
                input_refs=output_refs,
                generator_code_version=GENERATOR_CODE_VERSION,
                template_version=graph.template_version_label,
            )
            selected_hash = canonical_hash({"stable_key": stable_key, "fingerprint": fingerprint})
        output_refs[stable_key] = selected_hash
        build_node = BuildNode(
            id=uuid.uuid4(),
            build_id=build.id,
            graph_node_id=graph_node.id,
            stable_key=stable_key,
            fingerprint=fingerprint,
            status=str(BuildNodeStatus.PASSED),
            resolution="GENERATED",
            selected_asset_set_hash=selected_hash,
            reuse_proof_json=(
                {}
                if graph_node.validation_policy_id is None
                else {
                    "validations_current": True,
                    "validation_policy_id": str(graph_node.validation_policy_id),
                    "validation_ids": [str(uuid.uuid4())],
                }
            ),
            version=1,
        )
        session.add(build_node)
        await session.flush()
        if compiled_node.node_type.is_source:
            continue
        attempt = Attempt(
            id=uuid.uuid4(),
            build_node_id=build_node.id,
            attempt_no=1,
            mechanism="PRIMARY",
            provider="test-provider",
            model="test-model",
            idempotency_key=canonical_hash({"build_node_id": str(build_node.id), "slot": 1}),
            status="SUCCEEDED",
            completed_at=datetime.now(UTC),
        )
        asset = Asset(
            id=uuid.uuid4(),
            organization_id=owner.organization_id,
            sha256=selected_hash,
            size_bytes=2048,
            mime_type="application/octet-stream",
            media_kind="BINARY",
            b2_bucket="test-work",
            b2_key=f"test/{selected_hash}.bin",
            verified_at=datetime.now(UTC),
        )
        session.add_all([attempt, asset])
        await session.flush()
        session.add(
            AttemptAsset(
                id=uuid.uuid4(),
                attempt_id=attempt.id,
                asset_id=asset.id,
                role="primary",
                ordinal=0,
                selected=True,
            )
        )
        build_node.selected_attempt_id = attempt.id
    await session.flush()
    return project, revision, graph_row, build


async def preview_legal_change(
    session: AsyncSession, owner: Principal
) -> tuple[
    ChangeImpactService,
    ProjectResponse,
    ProjectRevision,
    GraphRevision,
    Build,
    ChangeSetResponse,
    ImpactPlanResponse,
]:
    project, revision, graph_row, baseline = await seed_completed_orbit(session, owner)
    service = ChangeImpactService(session, environment=COMPLETE_ENV)
    change_set = await service.create(
        project_id=project.id,
        principal=owner,
        request=ChangeSetCreateRequest(
            base_revision_id=revision.id,
            patch=legal_patch("no added sugar"),
        ),
    )
    plan = await service.impact(change_set_id=change_set.id, principal=owner)
    return service, project, revision, graph_row, baseline, change_set, plan


async def test_impact_is_side_effect_free_and_derives_exact_four(
    session: AsyncSession, owner: Principal
) -> None:
    service, project, _, _, _, change_set, plan = await preview_legal_change(session, owner)

    assert plan.summary.reuse == 14
    assert plan.summary.rebuild == 4
    assert plan.summary.blocked == 0
    assert plan.summary.provider_calls == 2
    assert plan.summary.estimated_cost_usd is None
    assert {node.stable_key for node in plan.nodes if node.decision == "REBUILD"} == set(
        EXPECTED_LEGAL_COPY_REBUILD
    )
    assert (
        await session.scalar(
            select(func.count(ProjectRevision.id)).where(ProjectRevision.project_id == project.id)
        )
        == 1
    )
    assert (
        await session.scalar(select(func.count(Build.id)).where(Build.project_id == project.id))
        == 1
    )
    assert (
        await session.scalar(
            select(func.count(ImpactPlanRow.id)).where(ImpactPlanRow.change_set_id == change_set.id)
        )
        == 1
    )

    repeated = await service.impact(change_set_id=change_set.id, principal=owner)
    assert repeated.plan_id == plan.plan_id
    assert repeated.plan_hash == plan.plan_hash


async def test_commit_is_atomic_queues_only_the_first_ready_rebuild(
    session: AsyncSession, owner: Principal
) -> None:
    service, project, _, _, _, change_set, plan = await preview_legal_change(session, owner)
    result = await service.commit(
        plan_id=plan.plan_id,
        principal=owner,
        request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
        idempotency_key="legal-copy-build-001",
    )

    assert result.status == "QUEUED"
    assert result.total_nodes == 18
    assert result.reused_nodes == 14
    assert result.rebuilt_nodes == 4
    nodes = (
        await session.scalars(select(BuildNode).where(BuildNode.build_id == result.build_id))
    ).all()
    assert sum(node.status == "REUSED" for node in nodes) == 14
    queued = [node for node in nodes if node.status == "QUEUED"]
    pending = [node for node in nodes if node.status == "PENDING"]
    assert [node.stable_key for node in queued] == ["copy.pack"]
    assert {node.stable_key for node in pending} == {
        "audio.narration",
        "graphic.end_card",
        "compose.delivery_package",
    }
    work = (
        await session.scalars(select(WorkItem).where(WorkItem.build_id == result.build_id))
    ).all()
    assert len(work) == 1
    assert work[0].target_id == queued[0].id
    stored_change = await session.get(ChangeSet, change_set.id)
    assert stored_change is not None and stored_change.status == "COMMITTED"
    stored_project = await session.get(Project, project.id)
    assert stored_project is not None
    assert stored_project.active_revision_id == result.project_revision_id

    replay = await service.commit(
        plan_id=plan.plan_id,
        principal=owner,
        request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
        idempotency_key="legal-copy-build-001",
    )
    assert replay.build_id == result.build_id
    assert (
        await session.scalar(
            select(func.count(Build.id)).where(Build.impact_plan_id == plan.plan_id)
        )
        == 1
    )

    with pytest.raises(IdempotencyConflictError):
        await service.commit(
            plan_id=plan.plan_id,
            principal=owner,
            request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
            idempotency_key="different-build-key",
        )


async def complete_rebuilt_nodes(session: AsyncSession, owner: Principal, build_id: uuid.UUID):
    """Finish a committed build the way the worker would.

    Nodes the commit marked REUSED already point at the previous build's attempt.
    Only the rebuilt ones need an attempt, a verified asset, and current gates.
    """
    rows = (
        await session.execute(
            select(BuildNode, GraphNode)
            .join(GraphNode, GraphNode.id == BuildNode.graph_node_id)
            .where(BuildNode.build_id == build_id)
        )
    ).all()
    for build_node, graph_node in rows:
        if build_node.status == str(BuildNodeStatus.REUSED):
            continue
        if graph_node.node_type.startswith("SOURCE_"):
            build_node.status = str(BuildNodeStatus.PASSED)
            continue
        selected_hash = canonical_hash(
            {"stable_key": build_node.stable_key, "fingerprint": build_node.fingerprint}
        )
        attempt = Attempt(
            id=uuid.uuid4(),
            build_node_id=build_node.id,
            attempt_no=1,
            mechanism="PRIMARY",
            provider="test-provider",
            model="test-model",
            idempotency_key=canonical_hash({"build_node_id": str(build_node.id), "slot": 1}),
            status="SUCCEEDED",
            completed_at=datetime.now(UTC),
        )
        asset = Asset(
            id=uuid.uuid4(),
            organization_id=owner.organization_id,
            sha256=selected_hash,
            size_bytes=2048,
            mime_type="application/octet-stream",
            media_kind="BINARY",
            b2_bucket="test-work",
            b2_key=f"test/{selected_hash}.bin",
            verified_at=datetime.now(UTC),
        )
        session.add_all([attempt, asset])
        await session.flush()
        session.add(
            AttemptAsset(
                id=uuid.uuid4(),
                attempt_id=attempt.id,
                asset_id=asset.id,
                role="primary",
                ordinal=0,
                selected=True,
            )
        )
        build_node.status = str(BuildNodeStatus.PASSED)
        build_node.selected_attempt_id = attempt.id
        build_node.selected_asset_set_hash = selected_hash
        build_node.reuse_proof_json = (
            {}
            if graph_node.validation_policy_id is None
            else {
                "validations_current": True,
                "validation_policy_id": str(graph_node.validation_policy_id),
                "validation_ids": [str(uuid.uuid4())],
            }
        )
    build = await session.get(Build, build_id)
    assert build is not None
    build.status = "SUCCEEDED"
    build.completed_at = datetime.now(UTC)
    await session.flush()


async def test_second_incremental_build_reuses_the_first_builds_reused_nodes(
    session: AsyncSession, owner: Principal
) -> None:
    """A build that reused must itself be a valid baseline.

    Fourteen nodes of the first incremental build are REUSED and their selected
    attempts belong to the original baseline build, not to themselves. If either
    the accepted-status rule or the cross-build attempt lookup regressed, this
    second preview would report 18 rebuilds and every other build would
    regenerate the whole graph.
    """
    service, project, _, _, _, _, plan = await preview_legal_change(session, owner)
    first = await service.commit(
        plan_id=plan.plan_id,
        principal=owner,
        request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
        idempotency_key="second-incremental-first-build",
    )
    await complete_rebuilt_nodes(session, owner, first.build_id)

    reused = (
        await session.execute(
            select(func.count(BuildNode.id)).where(
                BuildNode.build_id == first.build_id,
                BuildNode.status == str(BuildNodeStatus.REUSED),
            )
        )
    ).scalar_one()
    assert reused == 14

    change_set = await service.create(
        project_id=project.id,
        principal=owner,
        request=ChangeSetCreateRequest(
            base_revision_id=first.project_revision_id,
            patch=legal_patch("sugar free"),
        ),
    )
    second = await service.impact(change_set_id=change_set.id, principal=owner)

    assert second.summary.reuse == 14
    assert second.summary.rebuild == 4
    assert second.summary.provider_calls == 2
    assert {node.stable_key for node in second.nodes if node.decision == "REBUILD"} == set(
        EXPECTED_LEGAL_COPY_REBUILD
    )


async def test_reused_node_may_not_borrow_an_unrelated_nodes_attempt(
    session: AsyncSession, owner: Principal
) -> None:
    """Accepting a cross-build attempt must not become "accept any attempt".

    The ancestor attempt is only proof when it belongs to a node carrying the same
    stable key and fingerprint; pointing at a different recipe's output must fail
    the reuse proof rather than silently select the wrong asset.
    """
    service, project, _, _, _, _, plan = await preview_legal_change(session, owner)
    first = await service.commit(
        plan_id=plan.plan_id,
        principal=owner,
        request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
        idempotency_key="borrowed-attempt-build",
    )
    await complete_rebuilt_nodes(session, owner, first.build_id)

    poster = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == first.build_id,
            BuildNode.stable_key == "image.poster",
        )
    )
    music = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == first.build_id,
            BuildNode.stable_key == "audio.music",
        )
    )
    assert poster is not None and music is not None
    poster.selected_attempt_id = music.selected_attempt_id
    await session.flush()

    change_set = await service.create(
        project_id=project.id,
        principal=owner,
        request=ChangeSetCreateRequest(
            base_revision_id=first.project_revision_id,
            patch=legal_patch("sugar free"),
        ),
    )
    second = await service.impact(change_set_id=change_set.id, principal=owner)

    decision = next(node for node in second.nodes if node.stable_key == "image.poster")
    assert decision.decision == "REBUILD"
    assert decision.reason_code == "CACHE_ASSET_MISSING"


async def test_editing_draft_invalidates_earlier_plan(
    session: AsyncSession, owner: Principal
) -> None:
    service, _, _, _, _, change_set, plan = await preview_legal_change(session, owner)
    await service.update(
        change_set_id=change_set.id,
        principal=owner,
        request=ChangeSetUpdateRequest(patch=legal_patch("sugar free")),
    )

    with pytest.raises(ImpactPlanStaleError, match="draft changed"):
        await service.commit(
            plan_id=plan.plan_id,
            principal=owner,
            request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
            idempotency_key="stale-plan-build",
        )


async def test_changed_reuse_evidence_invalidates_plan(
    session: AsyncSession, owner: Principal
) -> None:
    service, _, _, _, baseline, _, plan = await preview_legal_change(session, owner)
    poster = await session.scalar(
        select(BuildNode).where(
            BuildNode.build_id == baseline.id,
            BuildNode.stable_key == "image.poster",
        )
    )
    assert poster is not None
    poster.reuse_proof_json = {**(poster.reuse_proof_json or {}), "revoked": True}
    await session.flush()

    with pytest.raises(ImpactPlanStaleError, match="reuse evidence changed"):
        await service.commit(
            plan_id=plan.plan_id,
            principal=owner,
            request=ImpactCommitRequest(plan_hash=plan.plan_hash, start_build=True),
            idempotency_key="revoked-evidence-build",
        )
