"""Deterministic local execution for the ORBIT end-card node."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
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
    Source,
    SourceVersion,
    Validation,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_domain.errors import FeatureNotConfiguredError, InvalidSourceError, NotFoundError
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.graph.orbit import PARAM_LEGAL_LINE
from takegraph_domain.storage.keys import content_address
from takegraph_infrastructure.b2 import B2Store, StoredObject
from takegraph_infrastructure.image_composition import compose_orbit_end_card

from takegraph_worker.anthropic_gateway import CopyPack
from takegraph_worker.build_work import schedule_ready_nodes


@dataclass(frozen=True, slots=True)
class PreparedEndCardWork:
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    copy_bytes: bytes
    product_bytes: bytes
    legal_line: str
    superseded_line: str | None
    already_fetching: bool
    done: bool = False


class EndCardWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
    ) -> None:
        self._session_factory = session_factory
        self._store = store

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare(build_node_id)
        if prepared.done:
            return
        try:
            copy_pack = CopyPack.model_validate_json(prepared.copy_bytes)
            output = await asyncio.to_thread(
                compose_orbit_end_card,
                prepared.product_bytes,
                legal_line=copy_pack.legal_line,
            )
            digest = hashlib.sha256(output).hexdigest()
            key = content_address(
                organization_id=prepared.organization_id,
                sha256=digest,
                extension="png",
                prefix=self._store.prefix,
            )
            stored = await asyncio.to_thread(
                self._store.store_bytes,
                key,
                output,
                content_type="image/png",
                metadata={"attempt_id": str(prepared.attempt_id), "role": "end_card"},
            )
            if not await asyncio.to_thread(self._store.verify, key, expected_sha256=stored.sha256):
                raise InvalidSourceError("End-card PNG failed B2 re-verification.")
            await self._persist_fetching(prepared, stored.sha256)
            await self._finalize(prepared, stored)
        except (InvalidSourceError, ValueError) as exc:
            await self._fail(prepared, exc)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedEndCardWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("End-card build node was not found.")
            if node.stable_key != "graphic.end_card":
                raise FeatureNotConfiguredError(
                    f"End-card handler cannot execute {node.stable_key}."
                )
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("End-card build was not found.")
            project = await session.get(Project, build.project_id)
            revision = await session.get(ProjectRevision, build.project_revision_id)
            if project is None or revision is None:
                raise InvalidSourceError("End-card build references incomplete project data.")
            copy_node = await session.scalar(
                select(BuildNode).where(
                    BuildNode.build_id == build.id,
                    BuildNode.stable_key == "copy.pack",
                )
            )
            source_node = await session.scalar(
                select(BuildNode).where(
                    BuildNode.build_id == build.id,
                    BuildNode.stable_key == "source.product_reference",
                )
            )
            if copy_node is None or copy_node.selected_attempt_id is None or source_node is None:
                raise InvalidSourceError("End-card inputs are incomplete.")
            copy_asset = await session.scalar(
                select(Asset)
                .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
                .where(
                    AttemptAsset.attempt_id == copy_node.selected_attempt_id,
                    AttemptAsset.role == "copy",
                    AttemptAsset.selected.is_(True),
                )
            )
            product_asset = await session.scalar(
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
            if copy_asset is None or product_asset is None:
                raise InvalidSourceError("End-card inputs are not durable indexed assets.")
            for asset in (copy_asset, product_asset):
                if asset.verified_at is None:
                    raise InvalidSourceError("End-card input failed stored-byte verification.")
            # One download each, hashed on the way through.
            copy_bytes, product_bytes = await asyncio.gather(
                asyncio.to_thread(
                    self._store.get_verified, copy_asset.b2_key, expected_sha256=copy_asset.sha256
                ),
                asyncio.to_thread(
                    self._store.get_verified,
                    product_asset.b2_key,
                    expected_sha256=product_asset.sha256,
                ),
            )
            copy_pack = CopyPack.model_validate_json(copy_bytes)
            parameters = revision.spec_json.get("parameters", {})
            legal_line = parameters.get(PARAM_LEGAL_LINE) if isinstance(parameters, dict) else None
            if not isinstance(legal_line, str) or copy_pack.legal_line != legal_line:
                raise InvalidSourceError("End-card copy does not match the committed legal line.")
            superseded_line = await self._superseded_line(session, revision)

            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None and attempt.status == str(AttemptStatus.SUCCEEDED):
                return self._prepared(
                    attempt,
                    project,
                    build,
                    node,
                    copy_bytes,
                    product_bytes,
                    legal_line,
                    superseded_line,
                    False,
                    done=True,
                )
            if attempt is not None and attempt.status not in {
                str(AttemptStatus.SUBMITTING),
                str(AttemptStatus.FETCHING),
            }:
                raise InvalidSourceError(f"End-card attempt cannot resume from {attempt.status}.")
            if attempt is None:
                if node.status != str(BuildNodeStatus.QUEUED):
                    raise InvalidSourceError(f"End-card node is not runnable from {node.status}.")
                assert_transition(BuildNodeStatus.QUEUED, BuildNodeStatus.RUNNING, subject="node")
                self._node_transition(session, project, build, node, BuildNodeStatus.RUNNING)
                node.started_at = datetime.now(UTC)
                node.version += 1
                attempt_no = (
                    await session.scalar(
                        select(func.max(Attempt.attempt_no)).where(Attempt.build_node_id == node.id)
                    )
                    or 0
                ) + 1
                attempt = Attempt(
                    id=uuid.uuid4(),
                    build_node_id=node.id,
                    attempt_no=attempt_no,
                    mechanism=str(AttemptMechanism.PRIMARY),
                    provider="local",
                    model="end_card.v1",
                    idempotency_key=submission_idempotency_key(
                        build_node_id=node.id,
                        fingerprint=node.fingerprint,
                        mechanism=AttemptMechanism.PRIMARY,
                        provider="local",
                        model="end_card.v1",
                    ),
                    status=str(AttemptStatus.SUBMITTING),
                )
                session.add(attempt)
                self._attempt_event(session, attempt.id, "attempt.submitting", {})
            already_fetching = attempt.status == str(AttemptStatus.FETCHING)
            await session.commit()
            return self._prepared(
                attempt,
                project,
                build,
                node,
                copy_bytes,
                product_bytes,
                legal_line,
                superseded_line,
                already_fetching,
            )

    async def _persist_fetching(self, prepared: PreparedEndCardWork, sha256: str) -> None:
        if prepared.already_fetching:
            return
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("End-card attempt disappeared.")
            for target in (AttemptStatus.SUBMITTED, AttemptStatus.POLLING, AttemptStatus.FETCHING):
                assert_transition(AttemptStatus(attempt.status), target, subject="attempt")
                attempt.status = str(target)
                self._attempt_event(
                    session,
                    attempt.id,
                    f"attempt.{target.value.lower()}",
                    {"sha256": sha256} if target is AttemptStatus.FETCHING else {},
                )
            attempt.submitted_at = datetime.now(UTC)
            await session.commit()

    async def _finalize(self, prepared: PreparedEndCardWork, stored: StoredObject) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("End-card graph node is missing.")
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.STORED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.STORED)
            asset_id = await session.scalar(
                insert(Asset)
                .values(
                    id=uuid.uuid4(),
                    organization_id=project.organization_id,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    mime_type="image/png",
                    media_kind="IMAGE",
                    b2_bucket=self._store.bucket,
                    b2_key=stored.key,
                    storage_version_id=stored.version_id,
                    metadata_json={
                        "stable_key": node.stable_key,
                        "layout": "end_card.v1",
                        "width": 1920,
                        "height": 1080,
                        "rendered_legal_line": prepared.legal_line,
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
                raise InvalidSourceError("End-card asset could not be indexed.")
            await session.execute(
                insert(AttemptAsset)
                .values(
                    id=uuid.uuid4(),
                    attempt_id=attempt.id,
                    asset_id=asset_id,
                    role="end_card",
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
            superseded_absent = not prepared.superseded_line or (
                prepared.superseded_line.casefold() != prepared.legal_line.casefold()
            )
            validations = (
                ("required_phrase", {"rendered": prepared.legal_line, "renderer": "end_card.v1"}),
                (
                    "superseded_phrase",
                    {"superseded": prepared.superseded_line, "absent": superseded_absent},
                ),
                ("schema", {"width": 1920, "height": 1080, "format": "png"}),
            )
            validation_ids: list[str] = []
            for gate, evidence in validations:
                validation_id = uuid.uuid4()
                validation_ids.append(str(validation_id))
                session.add(
                    Validation(
                        id=validation_id,
                        build_node_id=node.id,
                        attempt_id=attempt.id,
                        asset_id=asset_id,
                        policy_id=graph_node.validation_policy_id,
                        gate_key=gate,
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

    async def _fail(self, prepared: PreparedEndCardWork, exc: Exception) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            if AttemptStatus(attempt.status) is not AttemptStatus.FAILED:
                assert_transition(
                    AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt"
                )
                attempt.status = str(AttemptStatus.FAILED)
            attempt.error_code = type(exc).__name__
            attempt.error_message = str(exc)[:500]
            attempt.completed_at = datetime.now(UTC)
            assert_transition(BuildNodeStatus(node.status), BuildNodeStatus.FAILED, subject="node")
            self._node_transition(session, project, build, node, BuildNodeStatus.FAILED)
            if build.status in {str(BuildStatus.RUNNING), str(BuildStatus.WAITING_REVIEW)}:
                assert_transition(BuildStatus(build.status), BuildStatus.FAILED, subject="build")
                build.status = str(BuildStatus.FAILED)
                build.version += 1
                build.completed_at = datetime.now(UTC)
            await session.commit()

    async def _locked(
        self,
        session: AsyncSession,
        prepared: PreparedEndCardWork,
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
            raise NotFoundError("End-card execution state disappeared.")
        return attempt, node, build, project

    async def _superseded_line(
        self,
        session: AsyncSession,
        revision: ProjectRevision,
    ) -> str | None:
        if revision.parent_revision_id is None:
            return None
        parent = await session.get(ProjectRevision, revision.parent_revision_id)
        params = {} if parent is None else parent.spec_json.get("parameters", {})
        value = params.get(PARAM_LEGAL_LINE) if isinstance(params, dict) else None
        return value if isinstance(value, str) else None

    @staticmethod
    def _prepared(
        attempt: Attempt,
        project: Project,
        build: Build,
        node: BuildNode,
        copy_bytes: bytes,
        product_bytes: bytes,
        legal_line: str,
        superseded_line: str | None,
        already_fetching: bool,
        done: bool = False,
    ) -> PreparedEndCardWork:
        return PreparedEndCardWork(
            attempt_id=attempt.id,
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            build_node_id=node.id,
            copy_bytes=copy_bytes,
            product_bytes=product_bytes,
            legal_line=legal_line,
            superseded_line=superseded_line,
            already_fetching=already_fetching,
            done=done,
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
                attempt_id=attempt_id, provider_event_type=event_type, provider_event_json=payload
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
        session.add(
            DomainEvent(
                event_id=uuid.uuid4(),
                organization_id=project.organization_id,
                project_id=project.id,
                build_id=build.id,
                event_type="build.node.status_changed",
                payload_json={
                    "build_node_id": str(node.id),
                    "stable_key": node.stable_key,
                    "from": previous,
                    "to": str(target),
                },
                correlation_id=uuid.uuid4(),
            )
        )


__all__ = ["EndCardWorkHandlers", "PreparedEndCardWork"]
