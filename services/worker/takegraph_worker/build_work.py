"""Crash-safe worker execution for incremental graph nodes."""

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
    GraphEdge,
    GraphNode,
    Project,
    ProjectRevision,
    ProviderPolicy,
    Validation,
)
from takegraph_api.queue import WorkQueue
from takegraph_domain.builds.scheduling import node_priority
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.canonical import canonical_bytes, canonical_hash
from takegraph_domain.enums import (
    AttemptMechanism,
    AttemptStatus,
    BuildNodeStatus,
    BuildStatus,
    NodeType,
)
from takegraph_domain.errors import (
    FeatureNotConfiguredError,
    InvalidSourceError,
    NotFoundError,
    ProviderAuthError,
    ProviderQuotaError,
)
from takegraph_domain.execution.idempotency import (
    submission_idempotency_key,
    work_item_dedupe_key,
)
from takegraph_domain.graph.orbit import DEFAULT_BRIEF_TEXT, PARAM_BRIEF_TEXT, PARAM_LEGAL_LINE
from takegraph_domain.storage.keys import content_address
from takegraph_infrastructure.b2 import B2Store, StoredObject

from takegraph_worker.anthropic_gateway import (
    AnthropicCopyGateway,
    CopyGenerationRequest,
    CopyGenerationResult,
    CopyGenerator,
)


@dataclass(frozen=True, slots=True)
class PreparedCopyWork:
    action: str
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    model: str
    brief: str
    legal_line: str
    superseded_line: str | None
    timeout_seconds: int
    recovered_result: CopyGenerationResult | None = None


class BuildWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        *,
        generator: CopyGenerator | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._environment = dict(os.environ if environment is None else environment)
        self._generator = generator

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare_copy(build_node_id)
        if prepared.action in {"DONE", "REVIEW"}:
            return
        result = prepared.recovered_result
        if result is None:
            generator = self._generator or AnthropicCopyGateway.from_env(self._environment)
            try:
                result = await generator.generate(
                    CopyGenerationRequest(
                        model=prepared.model,
                        brief=prepared.brief,
                        required_legal_line=prepared.legal_line,
                        timeout_seconds=prepared.timeout_seconds,
                    )
                )
            except (ProviderAuthError, ProviderQuotaError, FeatureNotConfiguredError) as exc:
                await self._terminal_provider_failure(prepared, exc)
                return
            except Exception as exc:
                # A timeout/connection loss may have happened after Anthropic
                # accepted the synchronous message request. Never resubmit it
                # automatically: the billable outcome is ambiguous (§13.2).
                await self._ambiguous_submission(prepared, exc)
                return
            await self._persist_provider_result(prepared, result)

        payload = canonical_bytes(result.output.model_dump(mode="json"))
        digest = canonical_hash(result.output.model_dump(mode="json"))
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
            metadata={"attempt_id": str(prepared.attempt_id), "role": "copy"},
        )
        if not await asyncio.to_thread(self._store.verify, key, expected_sha256=stored.sha256):
            raise InvalidSourceError("Stored copy-pack bytes failed B2 re-verification.")
        await self._finalize_copy(prepared, result, stored)

    async def _prepare_copy(self, build_node_id: uuid.UUID) -> PreparedCopyWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Build node was not found.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Build was not found.")
            project = await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            revision = await session.get(ProjectRevision, build.project_revision_id)
            if project is None or graph_node is None or revision is None:
                raise InvalidSourceError("Build execution references incomplete graph data.")
            if node.stable_key != "copy.pack" or graph_node.node_type != str(
                NodeType.STRUCTURED_TEXT
            ):
                raise FeatureNotConfiguredError(
                    f"Execution handler for {node.stable_key} is not implemented yet."
                )
            parameters = revision.spec_json.get("parameters", {})
            if not isinstance(parameters, dict):
                raise InvalidSourceError("Revision parameters are malformed.")
            legal_line = parameters.get(PARAM_LEGAL_LINE)
            brief = parameters.get(PARAM_BRIEF_TEXT, DEFAULT_BRIEF_TEXT)
            if not isinstance(legal_line, str) or not isinstance(brief, str):
                raise InvalidSourceError("Copy generation requires string brief and legal line.")
            superseded = await self._superseded_line(session, revision)
            policy = await session.get(ProviderPolicy, graph_node.provider_policy_id)
            provider, model, timeout = _resolved_policy(policy, self._environment)
            if provider != "anthropic":
                raise FeatureNotConfiguredError("copy.pack requires the Anthropic policy.")

            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None:
                if attempt.status == str(AttemptStatus.SUCCEEDED):
                    return self._prepared(
                        action="DONE",
                        attempt_id=attempt.id,
                        project=project,
                        build=build,
                        node=node,
                        model=model,
                        brief=brief,
                        legal_line=legal_line,
                        superseded_line=superseded,
                        timeout_seconds=timeout,
                    )
                if attempt.status == str(AttemptStatus.FETCHING):
                    recovered = await self._recover_result(session, attempt.id)
                    return self._prepared(
                        action="STORE",
                        attempt_id=attempt.id,
                        project=project,
                        build=build,
                        node=node,
                        model=model,
                        brief=brief,
                        legal_line=legal_line,
                        superseded_line=superseded,
                        timeout_seconds=timeout,
                        recovered_result=recovered,
                    )
                if attempt.status == str(AttemptStatus.SUBMITTING):
                    await self._mark_ambiguous_locked(session, project, build, node, attempt)
                    await session.commit()
                    return self._prepared(
                        action="REVIEW",
                        attempt_id=attempt.id,
                        project=project,
                        build=build,
                        node=node,
                        model=model,
                        brief=brief,
                        legal_line=legal_line,
                        superseded_line=superseded,
                        timeout_seconds=timeout,
                    )
                raise InvalidSourceError(
                    f"Existing copy attempt is in unsupported state {attempt.status}."
                )

            if build.status not in {str(BuildStatus.QUEUED), str(BuildStatus.RUNNING)}:
                raise InvalidSourceError(f"Build is not runnable from state {build.status}.")
            assert_transition(
                BuildNodeStatus(node.status), BuildNodeStatus.RUNNING, subject="build node"
            )
            if build.status == str(BuildStatus.QUEUED):
                assert_transition(BuildStatus.QUEUED, BuildStatus.RUNNING, subject="build")
                self._build_transition(session, project, build, BuildStatus.RUNNING)
                build.started_at = datetime.now(UTC)
            node.status = str(BuildNodeStatus.RUNNING)
            node.started_at = datetime.now(UTC)
            node.version += 1
            attempt_id = uuid.uuid4()
            attempt_no = (
                await session.scalar(
                    select(func.max(Attempt.attempt_no)).where(Attempt.build_node_id == node.id)
                )
                or 0
            ) + 1
            attempt = Attempt(
                id=attempt_id,
                build_node_id=node.id,
                attempt_no=attempt_no,
                mechanism=str(AttemptMechanism.PRIMARY),
                provider=provider,
                model=model,
                idempotency_key=submission_idempotency_key(
                    build_node_id=node.id,
                    fingerprint=node.fingerprint,
                    mechanism=AttemptMechanism.PRIMARY,
                    provider=provider,
                    model=model,
                ),
                status=str(AttemptStatus.SUBMITTING),
            )
            session.add(attempt)
            self._domain_event(
                session,
                project=project,
                build=build,
                event_type="build.node.status_changed",
                payload={
                    "build_node_id": str(node.id),
                    "stable_key": node.stable_key,
                    "from": "QUEUED",
                    "to": "RUNNING",
                },
            )
            self._attempt_event(session, attempt.id, "attempt.submitting", {})
            await session.commit()
            return self._prepared(
                action="GENERATE",
                attempt_id=attempt_id,
                project=project,
                build=build,
                node=node,
                model=model,
                brief=brief,
                legal_line=legal_line,
                superseded_line=superseded,
                timeout_seconds=timeout,
            )

    async def _persist_provider_result(
        self, prepared: PreparedCopyWork, result: CopyGenerationResult
    ) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Copy attempt disappeared after provider completion.")
            if attempt.status == str(AttemptStatus.FETCHING):
                return
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.SUBMITTED, subject="attempt"
            )
            for target in (
                AttemptStatus.SUBMITTED,
                AttemptStatus.POLLING,
                AttemptStatus.FETCHING,
            ):
                if AttemptStatus(attempt.status) is not target:
                    assert_transition(AttemptStatus(attempt.status), target, subject="attempt")
                attempt.status = str(target)
                self._attempt_event(
                    session,
                    attempt.id,
                    f"attempt.{target.value.lower()}",
                    (
                        {
                            "provider_message_id": result.provider_message_id,
                            "model": result.model,
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                            "output": result.output.model_dump(mode="json"),
                        }
                        if target is AttemptStatus.FETCHING
                        else {}
                    ),
                )
            attempt.submitted_at = datetime.now(UTC)
            await session.commit()

    async def _finalize_copy(
        self,
        prepared: PreparedCopyWork,
        result: CopyGenerationResult,
        stored: StoredObject,
    ) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Copy attempt was not found during finalization.")
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == attempt.build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Copy build node was not found during finalization.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            project = None if build is None else await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if build is None or project is None or graph_node is None:
                raise InvalidSourceError("Copy finalization references incomplete build data.")
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
                    metadata_json={"stable_key": node.stable_key, "role": "copy"},
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
                raise InvalidSourceError("Stored copy asset could not be indexed.")
            await session.execute(
                insert(AttemptAsset)
                .values(
                    id=uuid.uuid4(),
                    attempt_id=attempt.id,
                    asset_id=asset_id,
                    role="copy",
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
            validations = _copy_validations(result, prepared.legal_line, prepared.superseded_line)
            validation_ids: list[str] = []
            for gate_key, passed, evidence in validations:
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
                        status="PASS" if passed else "FAIL",
                        evidence_json=evidence,
                    )
                )

            assert_transition(BuildNodeStatus(node.status), BuildNodeStatus.STORING, subject="node")
            self._node_transition(session, project, build, node, BuildNodeStatus.STORING)
            assert_transition(
                BuildNodeStatus(node.status), BuildNodeStatus.VERIFYING, subject="node"
            )
            self._node_transition(session, project, build, node, BuildNodeStatus.VERIFYING)
            final_node_status = (
                BuildNodeStatus.PASSED
                if all(passed for _, passed, _ in validations)
                else BuildNodeStatus.FAILED
            )
            assert_transition(BuildNodeStatus(node.status), final_node_status, subject="node")
            self._node_transition(session, project, build, node, final_node_status)
            node.selected_attempt_id = attempt.id
            node.selected_asset_set_hash = stored.sha256
            node.reuse_proof_json = {
                "validations_current": final_node_status is BuildNodeStatus.PASSED,
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

            if final_node_status is BuildNodeStatus.PASSED:
                # The session deliberately disables autoflush. Readiness queries
                # must see this node's new PASSED state, not the earlier RUNNING
                # row still stored in PostgreSQL.
                await session.flush()
                await self._schedule_ready(session, build, project)
            else:
                assert_transition(BuildStatus(build.status), BuildStatus.FAILED, subject="build")
                self._build_transition(session, project, build, BuildStatus.FAILED)
                build.completed_at = datetime.now(UTC)
            await session.commit()

    async def _schedule_ready(self, session: AsyncSession, build: Build, project: Project) -> None:
        pending = (
            await session.execute(
                select(BuildNode, GraphNode)
                .join(GraphNode, GraphNode.id == BuildNode.graph_node_id)
                .where(BuildNode.build_id == build.id, BuildNode.status == "PENDING")
            )
        ).all()
        queue = WorkQueue(session)
        for node, graph_node in pending:
            predecessor_statuses = (
                await session.scalars(
                    select(BuildNode.status)
                    .join(GraphEdge, GraphEdge.from_node_id == BuildNode.graph_node_id)
                    .where(
                        BuildNode.build_id == build.id,
                        GraphEdge.to_node_id == graph_node.id,
                    )
                )
            ).all()
            if not all(
                BuildNodeStatus(value).satisfies_dependency for value in predecessor_statuses
            ):
                continue
            assert_transition(BuildNodeStatus.PENDING, BuildNodeStatus.QUEUED, subject="node")
            node.version += 1
            await queue.enqueue(
                kind="EXECUTE_BUILD_NODE",
                target_id=node.id,
                build_id=build.id,
                priority=node_priority(NodeType(graph_node.node_type)),
                dedupe_key=work_item_dedupe_key(
                    kind="EXECUTE_BUILD_NODE",
                    target_id=node.id,
                    discriminator=node.fingerprint,
                ),
                payload={"stable_key": node.stable_key, "trigger_source": "APPLICATION_COMMIT"},
            )
            self._node_transition(session, project, build, node, BuildNodeStatus.QUEUED)

    async def _recover_result(
        self, session: AsyncSession, attempt_id: uuid.UUID
    ) -> CopyGenerationResult:
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
            raise InvalidSourceError("FETCHING attempt has no persisted provider output.")
        return CopyGenerationResult.model_validate(event.provider_event_json)

    async def _superseded_line(
        self, session: AsyncSession, revision: ProjectRevision
    ) -> str | None:
        if revision.parent_revision_id is None:
            return None
        parent = await session.get(ProjectRevision, revision.parent_revision_id)
        if parent is None:
            return None
        parameters = parent.spec_json.get("parameters", {})
        value = parameters.get(PARAM_LEGAL_LINE) if isinstance(parameters, dict) else None
        return value if isinstance(value, str) else None

    async def _terminal_provider_failure(self, prepared: PreparedCopyWork, exc: Exception) -> None:
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

    async def _ambiguous_submission(self, prepared: PreparedCopyWork, exc: Exception) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked_execution(session, prepared)
            await self._mark_ambiguous_locked(session, project, build, node, attempt, exc=exc)
            await session.commit()

    async def _mark_ambiguous_locked(
        self,
        session: AsyncSession,
        project: Project,
        build: Build,
        node: BuildNode,
        attempt: Attempt,
        *,
        exc: Exception | None = None,
    ) -> None:
        assert_transition(AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt")
        attempt.status = str(AttemptStatus.FAILED)
        attempt.error_class = "INTERNAL"
        attempt.error_code = "AMBIGUOUS_SUBMISSION"
        attempt.error_message = "Provider submission outcome is ambiguous; manual review required."
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
            {"error_type": None if exc is None else type(exc).__name__},
        )

    @staticmethod
    def _prepared(
        *,
        action: str,
        attempt_id: uuid.UUID,
        project: Project,
        build: Build,
        node: BuildNode,
        model: str,
        brief: str,
        legal_line: str,
        superseded_line: str | None,
        timeout_seconds: int,
        recovered_result: CopyGenerationResult | None = None,
    ) -> PreparedCopyWork:
        return PreparedCopyWork(
            action=action,
            attempt_id=attempt_id,
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            build_node_id=node.id,
            model=model,
            brief=brief,
            legal_line=legal_line,
            superseded_line=superseded_line,
            timeout_seconds=timeout_seconds,
            recovered_result=recovered_result,
        )

    async def _locked_execution(
        self, session: AsyncSession, prepared: PreparedCopyWork
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
            raise NotFoundError("Execution state disappeared while handling provider result.")
        return attempt, node, build, project

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
        *,
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
            project=project,
            build=build,
            event_type="build.node.status_changed",
            payload={
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
            project=project,
            build=build,
            event_type="build.status_changed",
            payload={"from": previous, "to": str(target)},
        )


def _resolved_policy(policy: ProviderPolicy | None, env: Mapping[str, str]) -> tuple[str, str, int]:
    if policy is None:
        raise FeatureNotConfiguredError("Build node has no provider policy.")
    primary = policy.definition_json.get("primary")
    if not isinstance(primary, dict):
        raise InvalidSourceError("Provider policy primary configuration is malformed.")
    provider = primary.get("provider")
    model = primary.get("model")
    timeout = primary.get("timeout_seconds")
    if not isinstance(provider, str) or not isinstance(model, str) or not isinstance(timeout, int):
        raise InvalidSourceError("Provider policy fields are malformed.")
    if model.startswith("${") and model.endswith("}"):
        variable = model[2:-1]
        model = env.get(variable, "")
        if not model:
            raise FeatureNotConfiguredError(f"Provider model requires {variable}.")
    return provider, model, timeout


def _copy_validations(
    result: CopyGenerationResult,
    required_line: str,
    superseded_line: str | None,
) -> tuple[tuple[str, bool, dict[str, object]], ...]:
    output = result.output
    combined = " ".join((output.legal_line, output.narration, *output.captions)).casefold()
    superseded_absent = (
        True
        if not superseded_line or superseded_line.casefold() == required_line.casefold()
        else superseded_line.casefold() not in combined
    )
    return (
        (
            "required_phrase",
            output.legal_line == required_line,
            {"expected": required_line, "actual": output.legal_line},
        ),
        (
            "superseded_phrase",
            superseded_absent,
            {"superseded_phrase": superseded_line, "absent": superseded_absent},
        ),
        ("schema", True, {"schema": "copy_pack.v1"}),
    )


__all__ = ["BuildWorkHandlers", "PreparedCopyWork"]
