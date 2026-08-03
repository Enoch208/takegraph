"""Recovery chain integration tests — PRD §5.5, UJ-05, AS-02.

The domain tests prove the *decision*. These prove the *application*: that a
parked node really is requeued, that the routing reason is really persisted, and
that a build which can recover does not die.

Real PostgreSQL, because the things that break here are transactional — a node
parked without a queue item is a build that hangs forever, and no in-memory
substitute would catch it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from takegraph_api.db.models import (
    Attempt,
    Build,
    BuildNode,
    DomainEvent,
    GraphNode,
    Organization,
    Project,
    ProviderPolicy,
    WorkItem,
)
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_worker.recovery_work import apply_recovery, plan_recovery

POLICY_DEFINITION = {
    "key": "orbit-video-v1",
    "primary": {
        "provider": "gmicloud",
        "model": "wan2.6-r2v",
        "timeout_seconds": 480,
        "same_provider_fallback_models": ["pixverse-v5.6-i2v"],
    },
    "cross_provider_fallbacks": [
        {
            "provider": "runway",
            "model": "gen-3",
            "timeout_seconds": 480,
            "required_credential": "RUNWAYML_API_SECRET",
        }
    ],
    "retry": {"max_transient_retries": 2, "base_delay_seconds": 2, "max_delay_seconds": 30},
    "budgets": {"max_total_attempts": 4, "max_elapsed_seconds": 900},
}


class Fixture:
    """Holds the ORM objects the tests act on, plus plain ids for teardown.

    The ids are captured eagerly because `_cleanup` rolls back first, which
    expires every ORM instance; reading `build.id` afterwards would trigger a
    lazy refresh outside the async greenlet and fail.
    """

    __slots__ = (
        "build",
        "node",
        "project",
        "attempt",
        "org_id",
        "policy_id",
        "build_id",
        "project_id",
    )

    def __init__(self, project, build, node, attempt, org_id, policy_id) -> None:
        self.project = project
        self.build = build
        self.node = node
        self.attempt = attempt
        self.org_id = org_id
        self.policy_id = policy_id
        self.build_id = build.id
        self.project_id = project.id


async def _cleanup(session, f: Fixture) -> None:
    """Remove everything the seed created.

    Necessary rather than tidy: other suites assert on global row counts, and
    §8.1 makes evidence tables ON DELETE RESTRICT, so nothing cascades. Deletion
    runs in reverse dependency order.
    """
    await session.rollback()
    for statement, params in (
        (
            "delete from attempts where build_node_id in"
            " (select id from build_nodes where build_id = :b)",
            {"b": f.build_id},
        ),
        ("delete from build_nodes where build_id = :b", {"b": f.build_id}),
        ("delete from work_items where build_id = :b", {"b": f.build_id}),
        ("delete from domain_events where build_id = :b", {"b": f.build_id}),
        ("delete from builds where id = :b", {"b": f.build_id}),
        (
            "delete from graph_nodes where graph_revision_id in ("
            " select gr.id from graph_revisions gr"
            " join project_revisions pr on pr.id = gr.project_revision_id"
            " where pr.project_id = :p)",
            {"p": f.project_id},
        ),
        (
            "delete from graph_revisions where project_revision_id in"
            " (select id from project_revisions where project_id = :p)",
            {"p": f.project_id},
        ),
        ("delete from project_revisions where project_id = :p", {"p": f.project_id}),
        ("delete from provider_policies where id = :i", {"i": f.policy_id}),
        ("delete from projects where id = :p", {"p": f.project_id}),
        ("delete from organizations where id = :o", {"o": f.org_id}),
    ):
        await session.execute(text(statement), params)
    await session.commit()


async def _seed(session, *, error_class: str, attempts: int = 1) -> Fixture:
    """A node whose latest attempt just failed, with a real provider policy."""
    suffix = uuid.uuid4().hex[:8]
    org = Organization(id=uuid.uuid4(), slug=f"recov-{suffix}", name="Recovery")
    project = Project(
        id=uuid.uuid4(),
        organization_id=org.id,
        slug=f"orbit-recov-{suffix}",
        name="ORBIT",
        is_demo=True,
    )
    policy = ProviderPolicy(
        id=uuid.uuid4(),
        organization_id=org.id,
        key=f"orbit-video-{suffix}",
        version=1,
        definition_json=POLICY_DEFINITION,
        canonical_hash="ab" * 32,
    )
    session.add_all([org, project, policy])
    await session.flush()

    # Minimal graph/build scaffolding — this test is about the recovery
    # transition, not compilation, so the graph rows only need to be valid.
    await session.execute(
        text(
            "insert into graph_templates (id, key, version, schema_version,"
            " definition_json, created_at) values (:id, :key, 1, '1', '{}', now())"
        ),
        {"id": uuid.uuid4(), "key": f"tpl-{suffix}"},
    )
    template_id = await session.scalar(
        text("select id from graph_templates where key = :key"), {"key": f"tpl-{suffix}"}
    )
    revision_id, graph_revision_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "insert into project_revisions (id, project_id, revision_no, spec_json,"
            " canonical_hash, created_at) values (:id, :pid, 1, '{}', :h, now())"
        ),
        {"id": revision_id, "pid": project.id, "h": "cd" * 32},
    )
    await session.execute(
        text(
            "insert into graph_revisions (id, project_revision_id, template_id, canonical_hash,"
            " compiler_version, compiled_at) values (:id, :rid, :tid, :h, '1', now())"
        ),
        {"id": graph_revision_id, "rid": revision_id, "tid": template_id, "h": "ef" * 32},
    )

    graph_node = GraphNode(
        id=uuid.uuid4(),
        graph_revision_id=graph_revision_id,
        stable_key="video.clip.03",
        node_type="VIDEO_GENERATION",
        spec_json={},
        spec_hash="11" * 32,
        provider_policy_id=policy.id,
        required=True,
        label="Shot 3",
    )
    session.add(graph_node)
    await session.flush()

    build = Build(
        id=uuid.uuid4(),
        project_id=project.id,
        project_revision_id=revision_id,
        graph_revision_id=graph_revision_id,
        status=str(BuildStatus.RUNNING),
        total_nodes=1,
    )
    node = BuildNode(
        id=uuid.uuid4(),
        build_id=build.id,
        graph_node_id=graph_node.id,
        stable_key="video.clip.03",
        fingerprint="22" * 32,
        status=str(BuildNodeStatus.RUNNING),
        started_at=datetime.now(UTC),
    )
    session.add(build)
    await session.flush()
    session.add(node)
    await session.flush()

    attempt = None
    for index in range(1, attempts + 1):
        attempt = Attempt(
            id=uuid.uuid4(),
            build_node_id=node.id,
            attempt_no=index,
            mechanism=str(
                AttemptMechanism.PRIMARY if index == 1 else AttemptMechanism.SAME_PROVIDER_RETRY
            ),
            provider="gmicloud",
            model="wan2.6-r2v",
            idempotency_key=f"key-{suffix}-{index}",
            status=str(AttemptStatus.FAILED),
            error_class=error_class,
            estimated_cost_usd=Decimal("0.05"),
        )
        session.add(attempt)
    await session.flush()
    return Fixture(project, build, node, attempt, org.id, policy.id)


@pytest.mark.asyncio
class TestRecoveryApplication:
    async def test_transient_failure_parks_the_node_for_retry(self, session) -> None:
        try:
            f = await _seed(session, error_class="TRANSIENT")
            decision = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            applied = await apply_recovery(
                session,
                build=f.build,
                project=f.project,
                node=f.node,
                failed_attempt=f.attempt,
                decision=decision,
            )
            await session.commit()

            assert applied is True
            assert f.node.status == str(BuildNodeStatus.RETRY_PENDING)
            assert f.node.reason_code == "TRANSIENT_RETRY"

        finally:
            await _cleanup(session, f)

    async def test_recovery_enqueues_work(self, session) -> None:
        """A parked node with no queue item is a build that hangs forever, so this
        is the assertion that matters most."""
        try:
            f = await _seed(session, error_class="TRANSIENT")
            decision = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            await apply_recovery(
                session,
                build=f.build,
                project=f.project,
                node=f.node,
                failed_attempt=f.attempt,
                decision=decision,
            )
            await session.commit()

            items = (
                (await session.execute(select(WorkItem).where(WorkItem.build_id == f.build.id)))
                .scalars()
                .all()
            )
            assert len(items) == 1
            assert items[0].target_id == f.node.id
            assert items[0].payload_json["recovery"]["parent_attempt_id"] == str(f.attempt.id)

        finally:
            await _cleanup(session, f)

    async def test_requeue_key_does_not_collide_with_the_failed_attempt(self, session) -> None:
        """Two successive recoveries must both schedule. A dedupe key that ignored
        the parent attempt would silently drop the second."""
        try:
            f = await _seed(session, error_class="TRANSIENT")
            first = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            await apply_recovery(
                session,
                build=f.build,
                project=f.project,
                node=f.node,
                failed_attempt=f.attempt,
                decision=first,
            )
            await session.commit()

            # A second, distinct failed attempt on the same node.
            second_attempt = Attempt(
                id=uuid.uuid4(),
                build_node_id=f.node.id,
                attempt_no=2,
                mechanism=str(AttemptMechanism.SAME_PROVIDER_RETRY),
                provider="gmicloud",
                model="wan2.6-r2v",
                idempotency_key=f"second-{uuid.uuid4().hex[:8]}",
                status=str(AttemptStatus.FAILED),
                error_class="TRANSIENT",
            )
            session.add(second_attempt)
            f.node.status = str(BuildNodeStatus.RUNNING)
            await session.flush()

            decision = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            await apply_recovery(
                session,
                build=f.build,
                project=f.project,
                node=f.node,
                failed_attempt=second_attempt,
                decision=decision,
            )
            await session.commit()

            items = (
                (await session.execute(select(WorkItem).where(WorkItem.build_id == f.build.id)))
                .scalars()
                .all()
            )
            assert len(items) == 2, "the second recovery must schedule its own work item"

        finally:
            await _cleanup(session, f)

    async def test_recovery_records_the_routing_reason(self, session) -> None:
        """§5.5 FR-PROV-006: the mechanism and target actually used must be
        reconstructable from persisted evidence."""
        try:
            f = await _seed(session, error_class="TRANSIENT", attempts=3)
            decision = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            await apply_recovery(
                session,
                build=f.build,
                project=f.project,
                node=f.node,
                failed_attempt=f.attempt,
                decision=decision,
            )
            await session.commit()

            event = await session.scalar(
                select(DomainEvent)
                .where(
                    DomainEvent.build_id == f.build.id,
                    DomainEvent.event_type == "build.node.recovery_scheduled",
                )
                .order_by(DomainEvent.sequence.desc())
            )
            assert event is not None
            payload = event.payload_json
            assert payload["stable_key"] == "video.clip.03"
            assert payload["reason_code"] == "SAME_PROVIDER_MODEL_FALLBACK"
            assert payload["model"] == "pixverse-v5.6-i2v"
            assert payload["parent_attempt_id"] == str(f.attempt.id)

        finally:
            await _cleanup(session, f)

    async def test_exhausted_transient_budget_falls_back_to_another_model(self, session) -> None:
        """After the configured transient retries, recovery escalates rather than
        spinning on a model that keeps failing."""
        try:
            f = await _seed(session, error_class="TRANSIENT", attempts=3)
            decision = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            assert decision.mechanism is AttemptMechanism.SAME_PROVIDER_MODEL_FALLBACK
            assert decision.model == "pixverse-v5.6-i2v"

        finally:
            await _cleanup(session, f)

    async def test_non_retryable_failure_is_not_applied(self, session) -> None:
        """§13.3: an auth failure is deterministic. apply_recovery returns False so
        the caller fails the node instead of scheduling a pointless retry."""
        try:
            f = await _seed(session, error_class="AUTH")
            decision = await plan_recovery(
                session, node=f.node, error_class="AUTH", current_model="wan2.6-r2v"
            )
            applied = await apply_recovery(
                session,
                build=f.build,
                project=f.project,
                node=f.node,
                failed_attempt=f.attempt,
                decision=decision,
            )
            await session.commit()

            assert applied is False
            assert f.node.status == str(BuildNodeStatus.RUNNING), "node left for the caller to fail"
            items = (
                (await session.execute(select(WorkItem).where(WorkItem.build_id == f.build.id)))
                .scalars()
                .all()
            )
            assert items == [], "a deterministic failure must not schedule more work"

        finally:
            await _cleanup(session, f)

    async def test_budget_exhaustion_reports_which_budget(self, session) -> None:
        """An operator seeing a stopped build needs to know whether it ran out of
        attempts, time or money."""
        try:
            f = await _seed(session, error_class="TRANSIENT", attempts=4)
            decision = await plan_recovery(
                session, node=f.node, error_class="TRANSIENT", current_model="wan2.6-r2v"
            )
            assert decision.should_retry is False
            assert decision.reason_code == "BUDGET_EXCEEDED"
            assert "attempt budget" in decision.reason

        finally:
            await _cleanup(session, f)
