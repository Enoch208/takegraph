"""Crash-safe execution for the ORBIT four-shot planning node."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from takegraph_api.db.models import (
    Asset,
    Attempt,
    AttemptAsset,
    AttemptEvent,
    Build,
    BuildNode,
    DomainEvent,
    GraphNode,
    Project,
    ProjectRevision,
    ProviderPolicy,
    Source,
    SourceVersion,
    Validation,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.canonical import canonical_bytes, canonical_hash
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_domain.errors import (
    FeatureNotConfiguredError,
    InvalidSourceError,
    NotFoundError,
    ProviderAuthError,
    ProviderQuotaError,
)
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.graph.orbit import DEFAULT_BRIEF_TEXT, PARAM_BRIEF_TEXT
from takegraph_domain.storage.keys import content_address
from takegraph_infrastructure.b2 import B2Store, StoredObject

from takegraph_worker.anthropic_plan_gateway import (
    AnthropicPlanGateway,
    PlanGenerationRequest,
    PlanGenerationResult,
    PlanGenerator,
)
from takegraph_worker.build_work import resolve_provider_policy, schedule_ready_nodes
from takegraph_worker.reentry import logical_attempt_slot, plan_reentry


def _ambiguous_message(exc: Exception | None) -> str:
    """A reviewer-facing reason that names the actual failure.

    Truncated because this is stored on the attempt and rendered in the node
    inspector, and a provider stack trace is not a reason.
    """
    base = "Anthropic shot-plan submission is ambiguous; review required."
    if exc is None:
        return base
    detail = str(exc).strip() or type(exc).__name__
    cause = exc.__cause__
    if cause is not None and str(cause).strip():
        detail = f"{detail} ({type(cause).__name__}: {str(cause).strip()})"
    return f"{base} {detail}"[:1000]


@dataclass(frozen=True, slots=True)
class PreparedPlanWork:
    action: str
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    model: str
    brief: str
    product_bytes: bytes
    product_mime: str
    product_sha256: str
    timeout_seconds: int
    idempotency_key: str
    recovered_result: PlanGenerationResult | None = None


class PlanWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        *,
        generator: PlanGenerator | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._environment = dict(os.environ if environment is None else environment)
        self._generator = generator

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare(build_node_id)
        if prepared.action in {"DONE", "REVIEW"}:
            return
        result = prepared.recovered_result
        if result is None:
            generator = self._generator or AnthropicPlanGateway.from_env(self._environment)
            try:
                result = await generator.generate(
                    PlanGenerationRequest(
                        model=prepared.model,
                        brief=prepared.brief,
                        product_reference_bytes=prepared.product_bytes,
                        product_reference_mime=prepared.product_mime,
                        product_reference_sha256=prepared.product_sha256,
                        timeout_seconds=prepared.timeout_seconds,
                    )
                )
            except (
                ProviderAuthError,
                ProviderQuotaError,
                FeatureNotConfiguredError,
                InvalidSourceError,
            ) as exc:
                await self._terminal_failure(prepared, exc)
                return
            except Exception as exc:
                await self._ambiguous_submission(prepared, exc)
                return
            await self._persist_provider_result(prepared, result)

        output_json = result.output.model_dump(mode="json")
        payload = canonical_bytes(output_json)
        digest = canonical_hash(output_json)
        key = content_address(
            organization_id=prepared.organization_id,
            sha256=digest,
            extension="json",
            prefix=self._store.prefix,
        )
        stored = await asyncio.to_thread(
            self._store.store_bytes,
            key,
            payload,
            content_type="application/json",
            metadata={"attempt_id": str(prepared.attempt_id), "role": "plan"},
        )
        if not await asyncio.to_thread(self._store.verify, key, expected_sha256=stored.sha256):
            raise InvalidSourceError("Stored shot plan failed B2 re-verification.")
        await self._finalize(prepared, result, stored)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedPlanWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Shot-plan build node was not found.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Shot-plan build was not found.")
            project = await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            revision = await session.get(ProjectRevision, build.project_revision_id)
            if project is None or graph_node is None or revision is None:
                raise InvalidSourceError("Shot planning references incomplete build data.")
            if node.stable_key != "plan.shots":
                raise FeatureNotConfiguredError(
                    f"Shot-plan handler cannot execute {node.stable_key}."
                )
            policy = await session.get(ProviderPolicy, graph_node.provider_policy_id)
            provider, model, timeout = resolve_provider_policy(policy, self._environment)
            if provider != "anthropic":
                raise FeatureNotConfiguredError("plan.shots requires Anthropic.")
            brief = self._brief(revision)
            product_asset = await self._product_asset(session, build, project)
            # One download, hashed on the way through — see B2Store.get_verified.
            product_bytes = await asyncio.to_thread(
                self._store.get_verified,
                product_asset.b2_key,
                expected_sha256=product_asset.sha256,
            )

            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            # Resolved before the resume branches below so a parked node reaches
            # a fresh submission instead of tripping the "unsupported state"
            # guard, which is what previously killed every recovery and retake.
            reentry = await plan_reentry(
                session,
                node=node,
                latest=attempt,
                provider=provider,
                model=model,
                timeout_seconds=timeout,
                subject="Shot-plan node",
            )
            if reentry is not None:
                provider, model, timeout = reentry.provider, reentry.model, reentry.timeout_seconds

            if attempt is not None:
                if attempt.status == str(AttemptStatus.SUCCEEDED):
                    return self._prepared(
                        "DONE",
                        attempt,
                        project,
                        build,
                        node,
                        model,
                        brief,
                        product_asset,
                        product_bytes,
                        timeout,
                    )
                if attempt.status == str(AttemptStatus.FETCHING):
                    recovered = await self._recover_result(session, attempt.id)
                    return self._prepared(
                        "STORE",
                        attempt,
                        project,
                        build,
                        node,
                        model,
                        brief,
                        product_asset,
                        product_bytes,
                        timeout,
                        recovered,
                    )
                if attempt.status == str(AttemptStatus.SUBMITTING):
                    self._mark_ambiguous(session, project, build, node, attempt)
                    await session.commit()
                    return self._prepared(
                        "REVIEW",
                        attempt,
                        project,
                        build,
                        node,
                        model,
                        brief,
                        product_asset,
                        product_bytes,
                        timeout,
                    )
                if reentry is None:
                    raise InvalidSourceError(
                        f"Existing shot-plan attempt is in unsupported state {attempt.status}."
                    )

            if node.status != str(BuildNodeStatus.QUEUED):
                raise InvalidSourceError(f"Shot-plan node is not runnable from {node.status}.")
            if build.status not in {str(BuildStatus.QUEUED), str(BuildStatus.RUNNING)}:
                raise InvalidSourceError(f"Shot-plan build is not runnable from {build.status}.")
            assert_transition(BuildNodeStatus.QUEUED, BuildNodeStatus.RUNNING, subject="node")
            self._node_transition(session, project, build, node, BuildNodeStatus.RUNNING)
            node.started_at = datetime.now(UTC)
            node.version += 1
            if build.status == str(BuildStatus.QUEUED):
                assert_transition(BuildStatus.QUEUED, BuildStatus.RUNNING, subject="build")
                self._build_transition(session, project, build, BuildStatus.RUNNING)
                build.started_at = datetime.now(UTC)
            attempt_no = (
                await session.scalar(
                    select(func.max(Attempt.attempt_no)).where(Attempt.build_node_id == node.id)
                )
                or 0
            ) + 1
            mechanism = reentry.mechanism if reentry else AttemptMechanism.PRIMARY
            attempt = Attempt(
                id=uuid.uuid4(),
                build_node_id=node.id,
                attempt_no=attempt_no,
                mechanism=str(mechanism),
                # §14.3 lineage: a recovery or retake points at the attempt it
                # supersedes, so the inspector shows a chain rather than a set of
                # unrelated tries.
                parent_attempt_id=reentry.parent_attempt_id if reentry else None,
                provider=provider,
                model=model,
                idempotency_key=submission_idempotency_key(
                    build_node_id=node.id,
                    fingerprint=node.fingerprint,
                    mechanism=mechanism,
                    provider=provider,
                    model=model,
                    logical_attempt_slot=await logical_attempt_slot(
                        session,
                        build_node_id=node.id,
                        mechanism=mechanism,
                        provider=provider,
                        model=model,
                    ),
                ),
                status=str(AttemptStatus.SUBMITTING),
            )
            session.add(attempt)
            self._attempt_event(session, attempt.id, "attempt.submitting", {})
            await session.commit()
            return self._prepared(
                "GENERATE",
                attempt,
                project,
                build,
                node,
                model,
                brief,
                product_asset,
                product_bytes,
                timeout,
            )

    @staticmethod
    def _brief(revision: ProjectRevision) -> str:
        parameters = revision.spec_json.get("parameters", {})
        brief = parameters.get(PARAM_BRIEF_TEXT, DEFAULT_BRIEF_TEXT)
        if not isinstance(brief, str):
            raise InvalidSourceError("Shot-plan creative brief must be a string.")
        return brief

    async def _product_asset(
        self,
        session: AsyncSession,
        build: Build,
        project: Project,
    ) -> Asset:
        source_node = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.id,
                BuildNode.stable_key == "source.product_reference",
            )
        )
        if source_node is None or source_node.selected_asset_set_hash is None:
            raise InvalidSourceError("Shot planning requires the selected product reference.")
        asset = await session.scalar(
            select(Asset)
            .join(SourceVersion, SourceVersion.asset_id == Asset.id)
            .join(Source, Source.id == SourceVersion.source_id)
            .where(
                Source.project_id == project.id,
                Source.stable_key == "source.product_reference",
                SourceVersion.content_hash == source_node.selected_asset_set_hash,
            )
            .order_by(SourceVersion.created_at.desc())
            .limit(1)
        )
        if asset is None or asset.verified_at is None:
            raise InvalidSourceError("Shot-plan product reference is not a durable asset.")
        return asset

    async def _persist_provider_result(
        self, prepared: PreparedPlanWork, result: PlanGenerationResult
    ) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Shot-plan attempt disappeared after provider completion.")
            if attempt.status == str(AttemptStatus.FETCHING):
                return
            for target in (
                AttemptStatus.SUBMITTED,
                AttemptStatus.POLLING,
                AttemptStatus.FETCHING,
            ):
                assert_transition(AttemptStatus(attempt.status), target, subject="attempt")
                attempt.status = str(target)
                self._attempt_event(
                    session,
                    attempt.id,
                    f"attempt.{target.value.lower()}",
                    result.model_dump(mode="json") if target is AttemptStatus.FETCHING else {},
                )
            attempt.submitted_at = datetime.now(UTC)
            await session.commit()

    async def _finalize(
        self,
        prepared: PreparedPlanWork,
        result: PlanGenerationResult,
        stored: StoredObject,
    ) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked_execution(session, prepared)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("Shot-plan validation policy is missing.")
            if attempt.status == str(AttemptStatus.SUCCEEDED):
                return
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.STORED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.STORED)
            self._attempt_event(session, attempt.id, "attempt.stored", {"sha256": stored.sha256})
            asset_id = await session.scalar(
                insert(Asset)
                .values(
                    id=uuid.uuid4(),
                    organization_id=project.organization_id,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    mime_type=stored.content_type,
                    media_kind="DOCUMENT",
                    b2_bucket=self._store.bucket,
                    b2_key=stored.key,
                    storage_version_id=stored.version_id,
                    metadata_json={
                        "stable_key": node.stable_key,
                        "role": "plan",
                        "schema": "shot_plan.v1",
                        "provider_message_id": result.provider_message_id,
                        "model": result.model,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                    },
                    verified_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=[Asset.organization_id, Asset.sha256])
                .returning(Asset.id)
            )
            if asset_id is None:
                asset_id = await session.scalar(
                    select(Asset.id).where(
                        Asset.organization_id == project.organization_id,
                        Asset.sha256 == stored.sha256,
                    )
                )
            if asset_id is None:
                raise InvalidSourceError("Shot-plan asset could not be indexed.")
            await session.execute(
                insert(AttemptAsset)
                .values(
                    id=uuid.uuid4(),
                    attempt_id=attempt.id,
                    asset_id=asset_id,
                    role="plan",
                    ordinal=0,
                    selected=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        AttemptAsset.attempt_id,
                        AttemptAsset.role,
                        AttemptAsset.ordinal,
                    ]
                )
            )
            validations = (
                ("schema", {"schema": result.output.schema_version}),
                ("shot_count", {"expected": 4, "actual": len(result.output.shots)}),
                (
                    "duration",
                    {
                        "expected_seconds": 16,
                        "actual_seconds": sum(
                            shot.duration_seconds for shot in result.output.shots
                        ),
                    },
                ),
                (
                    "storage_hash",
                    {"sha256": stored.sha256, "source_sha256": prepared.product_sha256},
                ),
            )
            validation_ids: list[str] = []
            for gate_key, evidence in validations:
                validation_id = uuid.uuid4()
                validation_ids.append(str(validation_id))
                session.add(
                    Validation(
                        id=validation_id,
                        build_node_id=node.id,
                        attempt_id=attempt.id,
                        asset_id=asset_id,
                        policy_id=graph_node.validation_policy_id,
                        gate_key=gate_key,
                        gate_version="1",
                        status="PASS",
                        evidence_json=evidence,
                    )
                )
            for target in (
                BuildNodeStatus.STORING,
                BuildNodeStatus.VERIFYING,
                BuildNodeStatus.PASSED,
            ):
                assert_transition(BuildNodeStatus(node.status), target, subject="node")
                self._node_transition(session, project, build, node, target)
            node.selected_attempt_id = attempt.id
            node.selected_asset_set_hash = stored.sha256
            node.reuse_proof_json = {
                "validations_current": True,
                "validation_policy_id": str(graph_node.validation_policy_id),
                "validation_ids": validation_ids,
                "asset_ids": [str(asset_id)],
            }
            node.completed_at = datetime.now(UTC)
            node.version += 1
            assert_transition(AttemptStatus.STORED, AttemptStatus.SUCCEEDED, subject="attempt")
            attempt.status = str(AttemptStatus.SUCCEEDED)
            attempt.completed_at = datetime.now(UTC)
            self._attempt_event(session, attempt.id, "attempt.succeeded", {})
            await session.flush()
            await schedule_ready_nodes(session, build, project)
            await session.commit()

    async def _terminal_failure(self, prepared: PreparedPlanWork, exc: Exception) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked_execution(session, prepared)
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.FAILED)
            attempt.error_code = type(exc).__name__
            attempt.error_message = str(exc)[:500]
            attempt.completed_at = datetime.now(UTC)
            assert_transition(BuildNodeStatus(node.status), BuildNodeStatus.FAILED, subject="node")
            self._node_transition(session, project, build, node, BuildNodeStatus.FAILED)
            assert_transition(BuildStatus(build.status), BuildStatus.FAILED, subject="build")
            self._build_transition(session, project, build, BuildStatus.FAILED)
            build.completed_at = datetime.now(UTC)
            await session.commit()

    async def _ambiguous_submission(self, prepared: PreparedPlanWork, exc: Exception) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked_execution(session, prepared)
            self._mark_ambiguous(session, project, build, node, attempt, exc)
            await session.commit()

    def _mark_ambiguous(
        self,
        session: AsyncSession,
        project: Project,
        build: Build,
        node: BuildNode,
        attempt: Attempt,
        exc: Exception | None = None,
    ) -> None:
        assert_transition(AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt")
        attempt.status = str(AttemptStatus.FAILED)
        attempt.error_class = "INTERNAL"
        attempt.error_code = "AMBIGUOUS_SUBMISSION"
        # Carry the underlying cause. The reviewer this parks the node for has to
        # decide between PASS, FAIL and RETAKE, and "review required" with the
        # cause discarded gives them nothing to decide on — §16.6 expects the
        # person overriding an automatic result to be able to justify it.
        attempt.error_message = _ambiguous_message(exc)
        attempt.completed_at = datetime.now(UTC)
        assert_transition(
            BuildNodeStatus(node.status), BuildNodeStatus.WAITING_REVIEW, subject="node"
        )
        self._node_transition(session, project, build, node, BuildNodeStatus.WAITING_REVIEW)
        node.reason_code = "AMBIGUOUS_SUBMISSION"
        node.reason = attempt.error_message
        node.version += 1
        if build.status == str(BuildStatus.RUNNING):
            assert_transition(BuildStatus.RUNNING, BuildStatus.WAITING_REVIEW, subject="build")
            self._build_transition(session, project, build, BuildStatus.WAITING_REVIEW)
        self._attempt_event(
            session,
            attempt.id,
            "attempt.ambiguous_submission",
            {
                "error_type": None if exc is None else type(exc).__name__,
                "error_detail": None if exc is None else str(exc)[:500],
                "cause_type": (
                    None
                    if exc is None or exc.__cause__ is None
                    else type(exc.__cause__).__name__
                ),
                "cause_detail": (
                    None if exc is None or exc.__cause__ is None else str(exc.__cause__)[:500]
                ),
            },
        )

    async def _recover_result(
        self, session: AsyncSession, attempt_id: uuid.UUID
    ) -> PlanGenerationResult:
        event = await session.scalar(
            select(AttemptEvent)
            .where(
                AttemptEvent.attempt_id == attempt_id,
                AttemptEvent.provider_event_type == "attempt.fetching",
            )
            .order_by(AttemptEvent.sequence.desc())
            .limit(1)
        )
        if event is None:
            raise InvalidSourceError("FETCHING shot-plan attempt has no persisted output.")
        return PlanGenerationResult.model_validate(event.provider_event_json)

    async def _locked_execution(
        self, session: AsyncSession, prepared: PreparedPlanWork
    ) -> tuple[Attempt, BuildNode, Build, Project]:
        attempt = await session.scalar(
            select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
        )
        node = await session.scalar(
            select(BuildNode).where(BuildNode.id == prepared.build_node_id).with_for_update()
        )
        build = await session.scalar(
            select(Build).where(Build.id == prepared.build_id).with_for_update()
        )
        project = await session.get(Project, prepared.project_id)
        if attempt is None or node is None or build is None or project is None:
            raise NotFoundError("Shot-plan execution state disappeared.")
        return attempt, node, build, project

    @staticmethod
    def _prepared(
        action: str,
        attempt: Attempt,
        project: Project,
        build: Build,
        node: BuildNode,
        model: str,
        brief: str,
        product_asset: Asset,
        product_bytes: bytes,
        timeout: int,
        recovered: PlanGenerationResult | None = None,
    ) -> PreparedPlanWork:
        return PreparedPlanWork(
            action=action,
            attempt_id=attempt.id,
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            build_node_id=node.id,
            model=model,
            brief=brief,
            product_bytes=product_bytes,
            product_mime=product_asset.mime_type,
            product_sha256=product_asset.sha256,
            timeout_seconds=timeout,
            idempotency_key=attempt.idempotency_key,
            recovered_result=recovered,
        )

    @staticmethod
    def _attempt_event(
        session: AsyncSession,
        attempt_id: uuid.UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            AttemptEvent(
                attempt_id=attempt_id,
                provider_event_type=event_type,
                provider_event_json=payload,
            )
        )

    @staticmethod
    def _domain_event(
        session: AsyncSession,
        project: Project,
        build: Build,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            DomainEvent(
                event_id=uuid.uuid4(),
                organization_id=project.organization_id,
                project_id=project.id,
                build_id=build.id,
                event_type=event_type,
                payload_json=payload,
                correlation_id=uuid.uuid4(),
            )
        )

    def _node_transition(
        self,
        session: AsyncSession,
        project: Project,
        build: Build,
        node: BuildNode,
        target: BuildNodeStatus,
    ) -> None:
        previous = node.status
        node.status = str(target)
        self._domain_event(
            session,
            project,
            build,
            "build.node.status_changed",
            {
                "build_node_id": str(node.id),
                "stable_key": node.stable_key,
                "from": previous,
                "to": str(target),
            },
        )

    def _build_transition(
        self,
        session: AsyncSession,
        project: Project,
        build: Build,
        target: BuildStatus,
    ) -> None:
        previous = build.status
        build.status = str(target)
        build.version += 1
        self._domain_event(
            session,
            project,
            build,
            "build.status_changed",
            {"from": previous, "to": str(target)},
        )


__all__ = ["PlanWorkHandlers", "PreparedPlanWork"]
