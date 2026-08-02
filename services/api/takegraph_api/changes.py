"""Draft changes, deterministic impact previews, and atomic plan commits.

The preview path is read-only apart from persisting the draft/immutable plan. It
never calls a provider or writes B2. The commit path locks the plan and project,
revalidates every binding, then creates the revision, graph, build nodes, reuse
proofs, initial queue work, event, and audit record in one database transaction.
"""

from __future__ import annotations

import copy
import hashlib
import os
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.canonical import JsonValue, canonical_hash, canonical_payload
from takegraph_domain.enums import BuildNodeStatus, BuildStatus, ImpactDecision, Role
from takegraph_domain.errors import (
    BuildNotRunnableError,
    ForbiddenError,
    IdempotencyConflictError,
    ImpactPlanStaleError,
    InvalidSourceError,
    NotFoundError,
)
from takegraph_domain.execution.idempotency import work_item_dedupe_key
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.impact import ImpactPlan, NodeImpact, compute_impact
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    ORBIT_TEMPLATE,
    PARAM_BRIEF_TEXT,
)
from takegraph_domain.graph.orbit_policies import orbit_policy_hashes
from takegraph_domain.graph.types import CompiledGraph, NodeCacheState

from takegraph_api.auth import require_permission
from takegraph_api.db.models import (
    Asset,
    Attempt,
    AttemptAsset,
    AuditLog,
    Build,
    BuildNode,
    ChangeSet,
    DomainEvent,
    GraphNode,
    GraphRevision,
    ImpactPlanRow,
    Project,
    ProjectRevision,
    Source,
    SourceVersion,
)
from takegraph_api.db.session import session_scope
from takegraph_api.graph_persistence import OrbitGraphRepository
from takegraph_api.queue import WorkQueue

GENERATOR_CODE_VERSION = "takegraph-generator-v1"
CHANGE_SET_TTL = timedelta(hours=24)
IMPACT_PLAN_TTL = timedelta(minutes=30)


class RevisionParametersPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legal_line: str | None = Field(default=None, min_length=1, max_length=500)
    brief_text: str | None = Field(default=None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def has_change(self) -> RevisionParametersPatch:
        if self.legal_line is None and self.brief_text is None:
            raise ValueError("at least one supported parameter must be supplied")
        return self


class RevisionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: RevisionParametersPatch


class ChangeSetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision_id: uuid.UUID | None = None
    patch: RevisionPatch


class ChangeSetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch: RevisionPatch


class ChangeSetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    base_revision_id: uuid.UUID
    patch: RevisionPatch
    status: str
    expires_at: datetime
    created_at: datetime


class ImpactSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reuse: int
    rebuild: int
    review: int
    blocked: int
    provider_calls: int
    estimated_cost_usd: str | None
    pricing_status: str


class ImpactPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    plan_id: uuid.UUID
    project_id: uuid.UUID
    base_revision_id: uuid.UUID
    proposed_revision_hash: str
    graph_revision_id: uuid.UUID
    nodes: list[NodeImpact]
    summary: ImpactSummaryResponse
    plan_hash: str
    expires_at: datetime


class ImpactCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_build: bool = True


class ImpactCommitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: uuid.UUID
    project_id: uuid.UUID
    project_revision_id: uuid.UUID
    graph_revision_id: uuid.UUID
    status: str
    total_nodes: int
    reused_nodes: int
    rebuilt_nodes: int


class ChangeImpactService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        environment: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._environment = dict(os.environ if environment is None else environment)
        self._now = now or datetime.now(UTC)

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        principal: Principal,
        request: ChangeSetCreateRequest,
    ) -> ChangeSetResponse:
        project = await self._project(project_id)
        self._authorize_draft(project, principal)
        base_id = request.base_revision_id or project.active_revision_id
        if base_id is None:
            raise InvalidSourceError("Project has no active revision to change.")
        base = await self._session.get(ProjectRevision, base_id)
        if base is None or base.project_id != project.id:
            raise NotFoundError("Base revision was not found for this project.")
        _apply_patch(base.spec_json, request.patch)
        row = ChangeSet(
            id=uuid.uuid4(),
            project_id=project.id,
            base_revision_id=base.id,
            patch_json=request.patch.model_dump(mode="json"),
            status="DRAFT",
            created_by=principal.actor_id,
            expires_at=self._now + CHANGE_SET_TTL,
        )
        self._session.add(row)
        await self._session.flush()
        self._audit(
            principal=principal,
            project=project,
            action="change_set.created",
            target_type="CHANGE_SET",
            target_id=row.id,
            after_ref=canonical_hash(row.patch_json),
        )
        await self._session.flush()
        return _change_set_response(row)

    async def update(
        self,
        *,
        change_set_id: uuid.UUID,
        principal: Principal,
        request: ChangeSetUpdateRequest,
    ) -> ChangeSetResponse:
        row = await self._change_set(change_set_id, lock=True)
        project = await self._project(row.project_id)
        self._authorize_draft(project, principal)
        if row.created_by != principal.actor_id and principal.role not in (Role.OWNER, Role.EDITOR):
            raise ForbiddenError("Only the draft creator or an editor may update this change set.")
        self._assert_draft(row)
        base = await self._session.get(ProjectRevision, row.base_revision_id)
        if base is None:
            raise InvalidSourceError("Change-set base revision cannot be resolved.")
        _apply_patch(base.spec_json, request.patch)
        before = canonical_hash(row.patch_json)
        row.patch_json = request.patch.model_dump(mode="json")
        self._audit(
            principal=principal,
            project=project,
            action="change_set.updated",
            target_type="CHANGE_SET",
            target_id=row.id,
            before_ref=before,
            after_ref=canonical_hash(row.patch_json),
        )
        await self._session.flush()
        return _change_set_response(row)

    async def impact(self, *, change_set_id: uuid.UUID, principal: Principal) -> ImpactPlanResponse:
        change_set = await self._change_set(change_set_id)
        project = await self._project(change_set.project_id)
        self._authorize_draft(project, principal)
        self._assert_draft(change_set)
        base_revision = await self._session.get(ProjectRevision, change_set.base_revision_id)
        if base_revision is None:
            raise InvalidSourceError("Change-set base revision cannot be resolved.")
        base_graph_row = await self._session.scalar(
            select(GraphRevision).where(GraphRevision.project_revision_id == base_revision.id)
        )
        if base_graph_row is None:
            raise InvalidSourceError("Base revision has no compiled graph.")

        patch = RevisionPatch.model_validate(change_set.patch_json)
        proposed_spec = _apply_patch(base_revision.spec_json, patch)
        proposed_hash = canonical_hash(proposed_spec)
        if proposed_hash == base_revision.canonical_hash:
            raise InvalidSourceError("The change set does not change the project revision.")
        proposed_graph = _compile(proposed_spec)
        base_states, baseline_build_id = await self._base_states(
            project=project,
            revision=base_revision,
            graph_revision=base_graph_row,
        )
        source_hashes = await self._source_hashes(project, proposed_spec)
        engine_plan = compute_impact(
            proposed_graph,
            base_states=base_states,
            source_content_hashes=source_hashes,
            generator_code_version=GENERATOR_CODE_VERSION,
            blocked_keys=_configuration_blocks(proposed_graph, self._environment),
        )
        expires_at = min(
            change_set.expires_at or (self._now + CHANGE_SET_TTL),
            change_set.created_at + IMPACT_PLAN_TTL,
        )
        policy_bindings: dict[str, JsonValue] = {
            key: value for key, value in orbit_policy_hashes().items()
        }
        binding: dict[str, JsonValue] = {
            "change_set_id": str(change_set.id),
            "patch_hash": canonical_hash(change_set.patch_json),
            "base_revision_hash": base_revision.canonical_hash,
            "base_graph_hash": base_graph_row.canonical_hash,
            "compiler_version": proposed_graph.compiler_version,
            "generator_code_version": GENERATOR_CODE_VERSION,
            "policy_hashes": policy_bindings,
            "provider_configuration": _provider_configuration(self._environment),
            "engine_plan_hash": engine_plan.plan_hash,
            "plan_evidence_hash": _plan_evidence_hash(engine_plan),
        }
        plan_hash = _bound_plan_hash(
            project_id=project.id,
            base_revision_id=base_revision.id,
            base_graph_revision_id=base_graph_row.id,
            proposed_revision_hash=proposed_hash,
            expires_at=expires_at,
            binding=binding,
        )
        existing = await self._session.scalar(
            select(ImpactPlanRow).where(ImpactPlanRow.plan_hash == plan_hash)
        )
        if existing is not None:
            return _public_plan(existing)

        plan_id = uuid.uuid4()
        response = _impact_response(
            plan_id=plan_id,
            project=project,
            base_revision=base_revision,
            base_graph=base_graph_row,
            proposed_hash=proposed_hash,
            engine_plan=engine_plan,
            plan_hash=plan_hash,
            expires_at=expires_at,
        )
        reuse_sources = {
            key: {
                "build_node_id": state.source_build_node_id,
                "selected_output_hash": state.selected_output_hash,
                "asset_ids": list(state.asset_ids),
                "validation_ids": list(state.validation_ids),
            }
            for key, state in base_states.items()
            if state.source_build_node_id is not None
        }
        row = ImpactPlanRow(
            id=plan_id,
            change_set_id=change_set.id,
            graph_revision_id=base_graph_row.id,
            plan_json={
                "public": response.model_dump(mode="json"),
                "binding": binding,
                "baseline_build_id": str(baseline_build_id),
                "reuse_sources": reuse_sources,
            },
            plan_hash=plan_hash,
            expires_at=expires_at,
        )
        self._session.add(row)
        self._audit(
            principal=principal,
            project=project,
            action="impact_plan.created",
            target_type="IMPACT_PLAN",
            target_id=plan_id,
            after_ref=plan_hash,
        )
        await self._session.flush()
        return response

    async def commit(
        self,
        *,
        plan_id: uuid.UUID,
        principal: Principal,
        request: ImpactCommitRequest,
        idempotency_key: str,
    ) -> ImpactCommitResponse:
        if not 8 <= len(idempotency_key) <= 200:
            raise IdempotencyConflictError("Idempotency-Key must contain 8 to 200 characters.")
        plan_row = await self._session.scalar(
            select(ImpactPlanRow).where(ImpactPlanRow.id == plan_id).with_for_update()
        )
        if plan_row is None:
            raise NotFoundError("Impact plan was not found.")
        if request.plan_hash != plan_row.plan_hash:
            raise ImpactPlanStaleError("The supplied plan hash does not match this impact plan.")
        change_set = await self._change_set(plan_row.change_set_id, lock=True)
        project = await self._project(change_set.project_id, lock=True)
        self._authorize_commit(project, principal)
        idempotency_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        existing = await self._existing_commit(plan_row.id, idempotency_digest)
        if existing is not None:
            return existing

        public = _public_plan(plan_row)
        binding = _binding(plan_row)
        self._assert_plan_current(
            plan_row=plan_row,
            change_set=change_set,
            project=project,
            public=public,
            binding=binding,
        )
        patch = RevisionPatch.model_validate(change_set.patch_json)
        base_revision = await self._session.get(ProjectRevision, change_set.base_revision_id)
        if base_revision is None:
            raise ImpactPlanStaleError("The base revision no longer exists.")
        proposed_spec = _apply_patch(base_revision.spec_json, patch)
        proposed_graph = _compile(proposed_spec)
        if canonical_hash(proposed_spec) != public.proposed_revision_hash:
            raise ImpactPlanStaleError("The proposed revision changed after impact preview.")
        # The public field is the project-spec hash; the engine graph hash remains
        # bound separately through engine_plan_hash in the immutable binding.
        if proposed_graph.compiler_version != binding["compiler_version"]:
            raise ImpactPlanStaleError("The graph compiler changed after impact preview.")
        base_graph = await self._session.get(GraphRevision, public.graph_revision_id)
        if base_graph is None or base_graph.project_revision_id != base_revision.id:
            raise ImpactPlanStaleError("The base graph changed after impact preview.")
        try:
            baseline_build_id = uuid.UUID(str(plan_row.plan_json["baseline_build_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ImpactPlanStaleError("Stored plan lost its baseline-build binding.") from exc
        current_states, _ = await self._base_states(
            project=project,
            revision=base_revision,
            graph_revision=base_graph,
            baseline_build_id=baseline_build_id,
        )
        current_engine_plan = compute_impact(
            proposed_graph,
            base_states=current_states,
            source_content_hashes=await self._source_hashes(project, proposed_spec),
            generator_code_version=GENERATOR_CODE_VERSION,
            blocked_keys=_configuration_blocks(proposed_graph, self._environment),
        )
        if current_engine_plan.plan_hash != binding.get("engine_plan_hash"):
            raise ImpactPlanStaleError(
                "Baseline reuse evidence changed after preview; compute a new impact plan."
            )

        revision = await self._target_revision(
            project=project,
            base_revision=base_revision,
            proposed_spec=proposed_spec,
            principal=principal,
        )
        persisted_graph = await OrbitGraphRepository(self._session).compile_revision(revision.id)
        if persisted_graph.canonical_hash != proposed_graph.canonical_hash:
            raise ImpactPlanStaleError("Compiled graph differs from the previewed graph.")

        decisions = {node.stable_key: node for node in public.nodes}
        if any(
            node.decision in (ImpactDecision.BLOCKED, ImpactDecision.REVIEW)
            for node in public.nodes
        ):
            raise BuildNotRunnableError(
                "Impact plan contains blocked or review-required nodes and cannot be committed."
            )
        graph_nodes = (
            await self._session.scalars(
                select(GraphNode).where(GraphNode.graph_revision_id == persisted_graph.id)
            )
        ).all()
        graph_nodes_by_key = {node.stable_key: node for node in graph_nodes}
        if len(graph_nodes_by_key) != len(proposed_graph.nodes):
            raise InvalidSourceError("Persisted graph node count differs from the compiled graph.")

        build = Build(
            id=uuid.uuid4(),
            project_id=project.id,
            project_revision_id=revision.id,
            graph_revision_id=persisted_graph.id,
            impact_plan_id=plan_row.id,
            status=str(BuildStatus.QUEUED if request.start_build else BuildStatus.PLANNED),
            total_nodes=len(public.nodes),
            reused_nodes=public.summary.reuse,
            rebuilt_nodes=public.summary.rebuild,
            is_fixture=False,
            version=1,
        )
        self._session.add(build)
        await self._session.flush()
        reuse_sources = {
            key: {
                "build_node_id": state.source_build_node_id,
                "selected_output_hash": state.selected_output_hash,
                "asset_ids": list(state.asset_ids),
                "validation_ids": list(state.validation_ids),
            }
            for key, state in current_states.items()
            if state.source_build_node_id is not None
        }
        ready_rebuilds: list[BuildNode] = []
        for stable_key in proposed_graph.topological_order:
            impact = decisions[stable_key]
            graph_node = graph_nodes_by_key[stable_key]
            build_node_id = uuid.uuid4()
            if impact.decision is ImpactDecision.REUSE:
                source = reuse_sources.get(stable_key, {})
                node_status = BuildNodeStatus.REUSED
                resolution = "REUSED_FROM_BUILD"
                selected_hash = source.get("selected_output_hash")
                proof = {
                    "source_build_node_id": source.get("build_node_id"),
                    "asset_ids": source.get("asset_ids", []),
                    "validation_ids": source.get("validation_ids", []),
                    "reason_code": str(impact.reason_code),
                }
            else:
                predecessors = [slot.from_key for slot in proposed_graph.by_key[stable_key].inputs]
                is_ready = request.start_build and all(
                    decisions[key].decision is ImpactDecision.REUSE for key in predecessors
                )
                node_status = BuildNodeStatus.QUEUED if is_ready else BuildNodeStatus.PENDING
                resolution = "REBUILT"
                selected_hash = None
                proof = None
            build_node = BuildNode(
                id=build_node_id,
                build_id=build.id,
                graph_node_id=graph_node.id,
                stable_key=stable_key,
                fingerprint=impact.new_fingerprint,
                status=str(node_status),
                resolution=resolution,
                reason_code=str(impact.reason_code),
                reason=impact.reason,
                selected_asset_set_hash=selected_hash,
                reuse_proof_json=proof,
                version=1,
            )
            self._session.add(build_node)
            if node_status is BuildNodeStatus.QUEUED:
                ready_rebuilds.append(build_node)
        await self._session.flush()
        queue = WorkQueue(self._session)
        for node in ready_rebuilds:
            await queue.enqueue(
                kind="EXECUTE_BUILD_NODE",
                target_id=node.id,
                build_id=build.id,
                dedupe_key=work_item_dedupe_key(
                    kind="EXECUTE_BUILD_NODE",
                    target_id=node.id,
                    discriminator=node.fingerprint,
                ),
                payload={"stable_key": node.stable_key, "trigger_source": "APPLICATION_COMMIT"},
            )

        project.active_revision_id = revision.id
        project.version += 1
        change_set.status = "COMMITTED"
        self._session.add(
            DomainEvent(
                event_id=uuid.uuid4(),
                organization_id=project.organization_id,
                project_id=project.id,
                build_id=build.id,
                event_type="build.created",
                payload_json={
                    "status": build.status,
                    "total_nodes": build.total_nodes,
                    "reused_nodes": build.reused_nodes,
                    "rebuilt_nodes": build.rebuilt_nodes,
                    "initial_queued_nodes": [node.stable_key for node in ready_rebuilds],
                },
                correlation_id=uuid.uuid4(),
            )
        )
        self._audit(
            principal=principal,
            project=project,
            action="impact_plan.committed",
            target_type="IMPACT_PLAN",
            target_id=plan_row.id,
            before_ref=plan_row.plan_hash,
            after_ref=str(build.id),
            reason=f"idempotency_sha256:{idempotency_digest}",
        )
        await self._session.flush()
        return _commit_response(build)

    async def _target_revision(
        self,
        *,
        project: Project,
        base_revision: ProjectRevision,
        proposed_spec: dict[str, JsonValue],
        principal: Principal,
    ) -> ProjectRevision:
        digest = canonical_hash(proposed_spec)
        existing = await self._session.scalar(
            select(ProjectRevision).where(
                ProjectRevision.project_id == project.id,
                ProjectRevision.canonical_hash == digest,
            )
        )
        if existing is not None:
            return existing
        revision_no = (
            await self._session.scalar(
                select(func.max(ProjectRevision.revision_no)).where(
                    ProjectRevision.project_id == project.id
                )
            )
            or 0
        ) + 1
        revision = ProjectRevision(
            id=uuid.uuid4(),
            project_id=project.id,
            revision_no=revision_no,
            parent_revision_id=base_revision.id,
            spec_json=proposed_spec,
            canonical_hash=digest,
            created_by=principal.actor_id,
        )
        self._session.add(revision)
        await self._session.flush()
        return revision

    async def _base_states(
        self,
        *,
        project: Project,
        revision: ProjectRevision,
        graph_revision: GraphRevision,
        baseline_build_id: uuid.UUID | None = None,
    ) -> tuple[dict[str, NodeCacheState], uuid.UUID]:
        statement = select(Build).where(
            Build.project_id == project.id,
            Build.project_revision_id == revision.id,
            Build.graph_revision_id == graph_revision.id,
            Build.status == str(BuildStatus.SUCCEEDED),
        )
        if baseline_build_id is not None:
            statement = statement.where(Build.id == baseline_build_id)
        build = await self._session.scalar(
            statement.order_by(Build.created_at.desc(), Build.id.desc()).limit(1)
        )
        if build is None:
            raise BuildNotRunnableError(
                "Impact preview requires a completed baseline build for the base revision."
            )
        rows = (
            await self._session.execute(
                select(BuildNode, GraphNode)
                .join(GraphNode, GraphNode.id == BuildNode.graph_node_id)
                .where(BuildNode.build_id == build.id)
            )
        ).all()
        if len(rows) != 18:
            raise InvalidSourceError("Completed baseline build does not contain the 18-node graph.")
        states: dict[str, NodeCacheState] = {}
        for build_node, graph_node in rows:
            proof = build_node.reuse_proof_json or {}
            assets_present, assets_verified, asset_ids = await self._asset_integrity(
                build_node=build_node,
                graph_node=graph_node,
                revision=revision,
            )
            try:
                node_status = BuildNodeStatus(build_node.status)
            except ValueError as exc:
                raise InvalidSourceError("Baseline build contains an unknown node status.") from exc
            validations_current = graph_node.validation_policy_id is None or (
                proof.get("validations_current") is True
                and proof.get("validation_policy_id") == str(graph_node.validation_policy_id)
            )
            states[build_node.stable_key] = NodeCacheState(
                stable_key=build_node.stable_key,
                fingerprint=build_node.fingerprint,
                status=node_status,
                selected_output_hash=build_node.selected_asset_set_hash,
                validations_current=validations_current,
                assets_present=assets_present,
                assets_verified=assets_verified,
                manually_approved=proof.get("manually_approved") is True,
                is_fixture=build.is_fixture,
                revoked=proof.get("revoked") is True,
                source_build_node_id=str(build_node.id),
                validation_ids=tuple(str(value) for value in proof.get("validation_ids", [])),
                asset_ids=asset_ids,
            )
        return states, build.id

    async def _asset_integrity(
        self,
        *,
        build_node: BuildNode,
        graph_node: GraphNode,
        revision: ProjectRevision,
    ) -> tuple[bool, bool, tuple[str, ...]]:
        if graph_node.node_type == "SOURCE_TEXT":
            expected = _brief_hash(revision.spec_json)
            matches = build_node.selected_asset_set_hash == expected
            return matches, matches, ()
        if graph_node.node_type == "SOURCE_IMAGE":
            row = await self._session.execute(
                select(SourceVersion, Asset)
                .join(Source, Source.id == SourceVersion.source_id)
                .outerjoin(Asset, Asset.id == SourceVersion.asset_id)
                .where(
                    Source.project_id == revision.project_id,
                    Source.stable_key == build_node.stable_key,
                )
                .order_by(SourceVersion.created_at.desc())
                .limit(1)
            )
            source_version, asset = row.one_or_none() or (None, None)
            if source_version is None or asset is None:
                return False, False, ()
            verified = (
                asset.verified_at is not None
                and source_version.content_hash == asset.sha256
                and build_node.selected_asset_set_hash == source_version.content_hash
            )
            return True, verified, (str(asset.id),)
        if build_node.selected_attempt_id is None:
            return False, False, ()
        attempt = await self._session.get(Attempt, build_node.selected_attempt_id)
        if (
            attempt is None
            or attempt.build_node_id != build_node.id
            or attempt.status != "SUCCEEDED"
        ):
            return False, False, ()
        rows = (
            await self._session.execute(
                select(AttemptAsset, Asset)
                .join(Asset, Asset.id == AttemptAsset.asset_id)
                .where(
                    AttemptAsset.attempt_id == attempt.id,
                    AttemptAsset.selected.is_(True),
                )
                .order_by(AttemptAsset.role, AttemptAsset.ordinal)
            )
        ).all()
        if not rows:
            return False, False, ()
        expected_hashes = {asset.sha256 for _, asset in rows}
        expected_hashes.add(
            canonical_hash(
                [
                    {
                        "role": link.role,
                        "ordinal": link.ordinal,
                        "sha256": asset.sha256,
                    }
                    for link, asset in rows
                ]
            )
        )
        verified = all(asset.verified_at is not None for _, asset in rows) and (
            build_node.selected_asset_set_hash in expected_hashes
        )
        return True, verified, tuple(str(asset.id) for _, asset in rows)

    async def _source_hashes(
        self, project: Project, proposed_spec: dict[str, JsonValue]
    ) -> dict[str, str]:
        product_hash = await self._session.scalar(
            select(SourceVersion.content_hash)
            .join(Source, Source.id == SourceVersion.source_id)
            .join(Asset, Asset.id == SourceVersion.asset_id)
            .where(
                Source.project_id == project.id,
                Source.stable_key == "source.product_reference",
                Asset.verified_at.is_not(None),
            )
            .order_by(SourceVersion.created_at.desc())
            .limit(1)
        )
        if product_hash is None:
            raise InvalidSourceError(
                "A verified source.product_reference is required before impact preview."
            )
        return {
            "source.brief": _brief_hash(proposed_spec),
            "source.product_reference": product_hash,
        }

    def _assert_plan_current(
        self,
        *,
        plan_row: ImpactPlanRow,
        change_set: ChangeSet,
        project: Project,
        public: ImpactPlanResponse,
        binding: dict[str, Any],
    ) -> None:
        if plan_row.expires_at is None or plan_row.expires_at <= self._now:
            raise ImpactPlanStaleError("The impact plan expired; preview the change again.")
        if change_set.status != "DRAFT":
            raise ImpactPlanStaleError("The change set is no longer a draft.")
        if project.active_revision_id != change_set.base_revision_id:
            raise ImpactPlanStaleError(
                "The project changed after this impact preview was created.",
                details={
                    "expected_revision_id": str(change_set.base_revision_id),
                    "current_revision_id": str(project.active_revision_id),
                },
            )
        if binding.get("patch_hash") != canonical_hash(change_set.patch_json):
            raise ImpactPlanStaleError("The draft changed after this impact preview was created.")
        if binding.get("policy_hashes") != orbit_policy_hashes():
            raise ImpactPlanStaleError("A provider or validation policy changed after preview.")
        if binding.get("provider_configuration") != _provider_configuration(self._environment):
            raise ImpactPlanStaleError("Provider model configuration changed after preview.")
        if public.plan_hash != plan_row.plan_hash:
            raise ImpactPlanStaleError("Stored impact-plan evidence failed its hash binding.")
        if binding.get("plan_evidence_hash") != canonical_hash(
            {
                "nodes": [node.model_dump(mode="json") for node in public.nodes],
                "summary": public.summary.model_dump(mode="json"),
            }
        ):
            raise ImpactPlanStaleError("Stored impact-plan evidence was modified after preview.")
        expected_hash = _bound_plan_hash(
            project_id=public.project_id,
            base_revision_id=public.base_revision_id,
            base_graph_revision_id=public.graph_revision_id,
            proposed_revision_hash=public.proposed_revision_hash,
            expires_at=public.expires_at,
            binding=binding,
        )
        if expected_hash != plan_row.plan_hash:
            raise ImpactPlanStaleError("Stored impact-plan binding failed verification.")

    async def _existing_commit(
        self, plan_id: uuid.UUID, idempotency_digest: str
    ) -> ImpactCommitResponse | None:
        build = await self._session.scalar(
            select(Build).where(Build.impact_plan_id == plan_id).limit(1)
        )
        if build is None:
            return None
        audit = await self._session.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "impact_plan.committed",
                AuditLog.target_id == plan_id,
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        if audit is None or audit.reason != f"idempotency_sha256:{idempotency_digest}":
            raise IdempotencyConflictError(
                "This impact plan was already committed with a different Idempotency-Key."
            )
        return _commit_response(build)

    async def _project(self, project_id: uuid.UUID, *, lock: bool = False) -> Project:
        statement = select(Project).where(Project.id == project_id)
        if lock:
            statement = statement.with_for_update()
        project = await self._session.scalar(statement)
        if project is None:
            raise NotFoundError("Project was not found.")
        return project

    async def _change_set(self, change_set_id: uuid.UUID, *, lock: bool = False) -> ChangeSet:
        statement = select(ChangeSet).where(ChangeSet.id == change_set_id)
        if lock:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise NotFoundError("Change set was not found.")
        return row

    def _assert_draft(self, row: ChangeSet) -> None:
        if row.status != "DRAFT":
            raise ImpactPlanStaleError("The change set is no longer editable.")
        if row.expires_at is None or row.expires_at <= self._now:
            raise ImpactPlanStaleError("The change set expired; create a new draft.")

    @staticmethod
    def _authorize_draft(project: Project, principal: Principal) -> None:
        permission = (
            Permission.EDIT_DEMO_DRAFT if principal.role is Role.GUEST else Permission.EDIT_SOURCES
        )
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=permission,
        )
        if principal.role is Role.GUEST and not project.is_demo:
            raise ForbiddenError("Guest drafts are limited to the scoped demo project.")

    @staticmethod
    def _authorize_commit(project: Project, principal: Principal) -> None:
        permission = (
            Permission.EDIT_DEMO_DRAFT if principal.role is Role.GUEST else Permission.RUN_BUILD
        )
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=permission,
        )
        if principal.role is Role.GUEST and not project.is_demo:
            raise ForbiddenError("Guest builds are limited to the scoped demo project.")

    def _audit(
        self,
        *,
        principal: Principal,
        project: Project,
        action: str,
        target_type: str,
        target_id: uuid.UUID,
        before_ref: str | None = None,
        after_ref: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._session.add(
            AuditLog(
                actor_user_id=principal.actor_id,
                actor_kind="SESSION",
                effective_role=str(principal.role),
                organization_id=project.organization_id,
                project_id=project.id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                before_ref=before_ref,
                after_ref=after_ref,
                reason=reason,
                correlation_id=uuid.uuid4(),
            )
        )


def _apply_patch(spec: dict[str, Any], patch: RevisionPatch) -> dict[str, JsonValue]:
    proposed: dict[str, Any] = copy.deepcopy(spec)
    parameters = proposed.setdefault("parameters", {})
    if not isinstance(parameters, dict):
        raise InvalidSourceError("Project revision parameters field must be an object.")
    parameters.update(patch.parameters.model_dump(exclude_none=True))
    canonical_payload(proposed)
    return proposed


def _compile(spec: dict[str, JsonValue]) -> CompiledGraph:
    parameters = spec.get("parameters")
    if not isinstance(parameters, dict):
        raise InvalidSourceError("Project revision parameters field must be an object.")
    return compile_graph(
        ORBIT_TEMPLATE,
        parameters=dict(parameters),
        policy_hashes=orbit_policy_hashes(),
    )


def _brief_hash(spec: dict[str, Any]) -> str:
    parameters = spec.get("parameters", {})
    value = (
        parameters.get(PARAM_BRIEF_TEXT, DEFAULT_BRIEF_TEXT)
        if isinstance(parameters, dict)
        else None
    )
    if not isinstance(value, str):
        raise InvalidSourceError("brief_text must be a string.")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _provider_configuration(env: Mapping[str, str]) -> dict[str, JsonValue]:
    return {
        "gmi_configured": bool(env.get("GMI_API_KEY")),
        "gmi_image_model": env.get("GMI_IMAGE_MODEL", ""),
        "gmi_video_model": env.get("GMI_VIDEO_MODEL", ""),
        "gmi_video_fallback_model": env.get("GMI_VIDEO_FALLBACK_MODEL", ""),
        "elevenlabs_configured": bool(env.get("ELEVENLABS_API_KEY")),
        "elevenlabs_music_model": env.get("ELEVENLABS_MUSIC_MODEL", ""),
        "elevenlabs_tts_model": env.get("ELEVENLABS_TTS_MODEL", ""),
        "elevenlabs_voice_id": env.get("ELEVENLABS_VOICE_ID", ""),
        "anthropic_configured": bool(env.get("ANTHROPIC_API_KEY")),
        "evaluator_model": env.get("EVALUATOR_MODEL", ""),
        "runway_configured": bool(env.get("RUNWAYML_API_SECRET")),
        "runway_video_model": env.get("RUNWAY_VIDEO_MODEL", ""),
    }


def _configuration_blocks(graph: CompiledGraph, env: Mapping[str, str]) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for node in graph.nodes:
        required: tuple[str, ...]
        if node.stable_key == "audio.narration":
            required = (
                "ELEVENLABS_API_KEY",
                "ELEVENLABS_TTS_MODEL",
                "ELEVENLABS_VOICE_ID",
            )
        elif node.stable_key == "audio.music":
            required = ("ELEVENLABS_API_KEY", "ELEVENLABS_MUSIC_MODEL")
        elif node.node_type.value in ("STRUCTURED_PLAN", "STRUCTURED_TEXT"):
            required = ("ANTHROPIC_API_KEY", "EVALUATOR_MODEL")
        elif node.node_type.value == "IMAGE_GENERATION":
            required = ("GMI_API_KEY", "GMI_IMAGE_MODEL")
        elif node.node_type.value == "VIDEO_GENERATION":
            required = ("GMI_API_KEY", "GMI_VIDEO_MODEL")
        else:
            required = ()
        missing = [name for name in required if not env.get(name)]
        if missing:
            blocks[node.stable_key] = f"Missing execution configuration: {', '.join(missing)}."
    return blocks


def _plan_evidence_hash(plan: ImpactPlan) -> str:
    return canonical_hash(
        {
            "nodes": [node.model_dump(mode="json") for node in plan.nodes],
            "summary": plan.summary.model_dump(mode="json"),
        }
    )


def _bound_plan_hash(
    *,
    project_id: uuid.UUID,
    base_revision_id: uuid.UUID,
    base_graph_revision_id: uuid.UUID,
    proposed_revision_hash: str,
    expires_at: datetime,
    binding: dict[str, Any],
) -> str:
    canonical_payload(binding)
    return canonical_hash(
        {
            "schema_version": "1",
            "project_id": str(project_id),
            "base_revision_id": str(base_revision_id),
            "base_graph_revision_id": str(base_graph_revision_id),
            "proposed_revision_hash": proposed_revision_hash,
            "expires_at": expires_at.isoformat(),
            "binding": binding,
        }
    )


def _impact_response(
    *,
    plan_id: uuid.UUID,
    project: Project,
    base_revision: ProjectRevision,
    base_graph: GraphRevision,
    proposed_hash: str,
    engine_plan: ImpactPlan,
    plan_hash: str,
    expires_at: datetime,
) -> ImpactPlanResponse:
    return ImpactPlanResponse(
        plan_id=plan_id,
        project_id=project.id,
        base_revision_id=base_revision.id,
        proposed_revision_hash=proposed_hash,
        graph_revision_id=base_graph.id,
        nodes=list(engine_plan.nodes),
        summary=ImpactSummaryResponse(
            reuse=engine_plan.summary.reuse,
            rebuild=engine_plan.summary.rebuild,
            review=engine_plan.summary.review,
            blocked=engine_plan.summary.blocked,
            provider_calls=engine_plan.summary.provider_calls,
            estimated_cost_usd=engine_plan.summary.estimated_cost_usd,
            pricing_status=str(engine_plan.summary.pricing_status),
        ),
        plan_hash=plan_hash,
        expires_at=expires_at,
    )


def _binding(row: ImpactPlanRow) -> dict[str, Any]:
    value = row.plan_json.get("binding")
    if not isinstance(value, dict):
        raise ImpactPlanStaleError("Stored impact plan is missing its immutable binding.")
    return value


def _public_plan(row: ImpactPlanRow) -> ImpactPlanResponse:
    value = row.plan_json.get("public")
    if not isinstance(value, dict):
        raise ImpactPlanStaleError("Stored impact plan is missing its public evidence.")
    return ImpactPlanResponse.model_validate(value)


def _change_set_response(row: ChangeSet) -> ChangeSetResponse:
    if row.expires_at is None:
        raise InvalidSourceError("Persisted change set is missing its expiry.")
    return ChangeSetResponse(
        id=row.id,
        project_id=row.project_id,
        base_revision_id=row.base_revision_id,
        patch=RevisionPatch.model_validate(row.patch_json),
        status=row.status,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


def _commit_response(build: Build) -> ImpactCommitResponse:
    return ImpactCommitResponse(
        build_id=build.id,
        project_id=build.project_id,
        project_revision_id=build.project_revision_id,
        graph_revision_id=build.graph_revision_id,
        status=build.status,
        total_nodes=build.total_nodes,
        reused_nodes=build.reused_nodes,
        rebuilt_nodes=build.rebuilt_nodes,
    )


DraftPrincipal = Annotated[Principal, Depends(require_permission(Permission.VIEW_PROJECT))]
router = APIRouter(prefix="/api/v1", tags=["changes", "builds"])


@router.post(
    "/projects/{project_id}/change-sets",
    response_model=ChangeSetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_change_set(
    project_id: uuid.UUID,
    request: ChangeSetCreateRequest,
    principal: DraftPrincipal,
) -> ChangeSetResponse:
    async with session_scope() as session:
        return await ChangeImpactService(session).create(
            project_id=project_id, principal=principal, request=request
        )


@router.patch("/change-sets/{change_set_id}", response_model=ChangeSetResponse)
async def update_change_set(
    change_set_id: uuid.UUID,
    request: ChangeSetUpdateRequest,
    principal: DraftPrincipal,
) -> ChangeSetResponse:
    async with session_scope() as session:
        return await ChangeImpactService(session).update(
            change_set_id=change_set_id, principal=principal, request=request
        )


@router.post("/change-sets/{change_set_id}/impact", response_model=ImpactPlanResponse)
async def create_impact_plan(
    change_set_id: uuid.UUID,
    principal: DraftPrincipal,
) -> ImpactPlanResponse:
    async with session_scope() as session:
        return await ChangeImpactService(session).impact(
            change_set_id=change_set_id, principal=principal
        )


@router.post(
    "/impact-plans/{plan_id}/commit",
    response_model=ImpactCommitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def commit_impact_plan(
    plan_id: uuid.UUID,
    request: ImpactCommitRequest,
    principal: DraftPrincipal,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ImpactCommitResponse:
    async with session_scope() as session:
        return await ChangeImpactService(session).commit(
            plan_id=plan_id,
            principal=principal,
            request=request,
            idempotency_key=idempotency_key,
        )


__all__ = [
    "ChangeImpactService",
    "ChangeSetCreateRequest",
    "ChangeSetResponse",
    "ChangeSetUpdateRequest",
    "ImpactCommitRequest",
    "ImpactCommitResponse",
    "ImpactPlanResponse",
    "RevisionPatch",
    "RevisionParametersPatch",
    "router",
]
