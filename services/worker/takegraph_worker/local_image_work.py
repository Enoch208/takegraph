"""Deterministic local execution for ORBIT cutout and poster nodes."""

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
    Source,
    SourceVersion,
    Validation,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_domain.errors import FeatureNotConfiguredError, InvalidSourceError, NotFoundError
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.storage.keys import content_address
from takegraph_infrastructure.b2 import B2Store, StoredObject
from takegraph_infrastructure.image_composition import (
    compose_orbit_poster,
    compose_product_cutout,
)
from takegraph_infrastructure.media import probe_media_bytes

from takegraph_worker.build_work import schedule_ready_nodes


@dataclass(frozen=True, slots=True)
class PreparedLocalImageWork:
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    stable_key: str
    model: str
    inputs: tuple[bytes, ...]
    done: bool = False


class LocalImageWorkHandlers:
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
            if prepared.stable_key == "transform.product_cutout":
                output = await asyncio.to_thread(
                    compose_product_cutout, prepared.inputs[0], feather_px=2
                )
                width, height = await self._dimensions(output)
                metadata: dict[str, object] = {
                    "transform": "background_removal.v1",
                    "feather_px": 2,
                    "width": width,
                    "height": height,
                    "has_alpha": True,
                }
            elif prepared.stable_key == "image.poster":
                output = await asyncio.to_thread(
                    compose_orbit_poster, prepared.inputs[0], prepared.inputs[1]
                )
                width, height = await self._dimensions(output)
                metadata = {
                    "layout": "poster.v1",
                    "width": width,
                    "height": height,
                }
            else:
                raise FeatureNotConfiguredError(
                    f"Local image handler cannot execute {prepared.stable_key}."
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
                metadata={
                    "attempt_id": str(prepared.attempt_id),
                    "stable_key": prepared.stable_key,
                },
            )
            if not await asyncio.to_thread(self._store.verify, key, expected_sha256=stored.sha256):
                raise InvalidSourceError("Local image failed B2 re-verification.")
            await self._persist_fetching(prepared, stored.sha256)
            await self._finalize(prepared, stored, metadata)
        except (InvalidSourceError, ValueError) as exc:
            await self._fail(prepared, exc)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedLocalImageWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Local image build node was not found.")
            if node.stable_key not in {"transform.product_cutout", "image.poster"}:
                raise FeatureNotConfiguredError(
                    f"Local image handler cannot execute {node.stable_key}."
                )
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Local image build was not found.")
            project = await session.get(Project, build.project_id)
            if project is None:
                raise InvalidSourceError("Local image build has no project.")
            product_asset = await self._product_asset(session, build, project)
            assets = [product_asset]
            if node.stable_key == "image.poster":
                assets.append(
                    await self._selected_attempt_asset(session, build, "image.keyframe.01")
                )
            for asset in assets:
                if asset.verified_at is None or not await asyncio.to_thread(
                    self._store.verify, asset.b2_key, expected_sha256=asset.sha256
                ):
                    raise InvalidSourceError("Local image input failed B2 verification.")
            inputs = tuple(
                await asyncio.gather(
                    *(asyncio.to_thread(self._store.get_bytes, asset.b2_key) for asset in assets)
                )
            )
            model = (
                "background_removal.v1"
                if node.stable_key == "transform.product_cutout"
                else "poster.v1"
            )
            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None and attempt.status == str(AttemptStatus.SUCCEEDED):
                return self._prepared(attempt, project, build, node, model, inputs, done=True)
            if attempt is not None and attempt.status not in {
                str(AttemptStatus.SUBMITTING),
                str(AttemptStatus.FETCHING),
            }:
                raise InvalidSourceError(
                    f"Local image attempt cannot resume from {attempt.status}."
                )
            if attempt is None:
                if node.status != str(BuildNodeStatus.QUEUED):
                    raise InvalidSourceError(
                        f"Local image node is not runnable from {node.status}."
                    )
                if build.status not in {str(BuildStatus.QUEUED), str(BuildStatus.RUNNING)}:
                    raise InvalidSourceError(
                        f"Local image build is not runnable from {build.status}."
                    )
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
                attempt = Attempt(
                    id=uuid.uuid4(),
                    build_node_id=node.id,
                    attempt_no=attempt_no,
                    mechanism=str(AttemptMechanism.PRIMARY),
                    provider="local",
                    model=model,
                    idempotency_key=submission_idempotency_key(
                        build_node_id=node.id,
                        fingerprint=node.fingerprint,
                        mechanism=AttemptMechanism.PRIMARY,
                        provider="local",
                        model=model,
                    ),
                    status=str(AttemptStatus.SUBMITTING),
                )
                session.add(attempt)
                self._attempt_event(session, attempt.id, "attempt.submitting", {})
            await session.commit()
            return self._prepared(attempt, project, build, node, model, inputs)

    async def _product_asset(self, session: AsyncSession, build: Build, project: Project) -> Asset:
        source_node = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.id,
                BuildNode.stable_key == "source.product_reference",
            )
        )
        if source_node is None or source_node.selected_asset_set_hash is None:
            raise InvalidSourceError("Local image requires the selected product reference.")
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
        if asset is None:
            raise InvalidSourceError("Product reference is not a durable indexed asset.")
        return asset

    @staticmethod
    async def _selected_attempt_asset(
        session: AsyncSession, build: Build, stable_key: str
    ) -> Asset:
        predecessor = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.id,
                BuildNode.stable_key == stable_key,
            )
        )
        if predecessor is None or predecessor.selected_attempt_id is None:
            raise InvalidSourceError(f"Local image requires selected {stable_key} output.")
        asset = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(
                AttemptAsset.attempt_id == predecessor.selected_attempt_id,
                AttemptAsset.selected.is_(True),
            )
            .order_by(AttemptAsset.ordinal)
            .limit(1)
        )
        if asset is None:
            raise InvalidSourceError(f"Selected {stable_key} asset is missing.")
        return asset

    async def _persist_fetching(self, prepared: PreparedLocalImageWork, sha256: str) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Local image attempt disappeared.")
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
                    {"sha256": sha256} if target is AttemptStatus.FETCHING else {},
                )
            attempt.submitted_at = datetime.now(UTC)
            await session.commit()

    async def _finalize(
        self,
        prepared: PreparedLocalImageWork,
        stored: StoredObject,
        metadata: dict[str, object],
    ) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("Local image graph node is missing.")
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
                    metadata_json={"stable_key": node.stable_key, **metadata},
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
                raise InvalidSourceError("Local image asset could not be indexed.")
            await session.execute(
                insert(AttemptAsset)
                .values(
                    id=uuid.uuid4(),
                    attempt_id=attempt.id,
                    asset_id=asset_id,
                    role="primary",
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
                ("storage_hash", {"sha256": stored.sha256}),
                (
                    "media_integrity",
                    {
                        "format": "png",
                        "width": metadata["width"],
                        "height": metadata["height"],
                    },
                ),
                ("schema", {"operation": prepared.model}),
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

    async def _fail(self, prepared: PreparedLocalImageWork, exc: Exception) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            if attempt.status != str(AttemptStatus.FAILED):
                assert_transition(
                    AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt"
                )
                attempt.status = str(AttemptStatus.FAILED)
            attempt.error_code = type(exc).__name__
            attempt.error_message = str(exc)[:500]
            attempt.completed_at = datetime.now(UTC)
            assert_transition(BuildNodeStatus(node.status), BuildNodeStatus.FAILED, subject="node")
            self._node_transition(session, project, build, node, BuildNodeStatus.FAILED)
            if build.status == str(BuildStatus.RUNNING):
                assert_transition(BuildStatus.RUNNING, BuildStatus.FAILED, subject="build")
                self._build_transition(session, project, build, BuildStatus.FAILED)
                build.completed_at = datetime.now(UTC)
            await session.commit()

    async def _locked(
        self, session: AsyncSession, prepared: PreparedLocalImageWork
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
            raise NotFoundError("Local image execution state disappeared.")
        return attempt, node, build, project

    @staticmethod
    async def _dimensions(data: bytes) -> tuple[int, int]:
        import tempfile
        from pathlib import Path

        probe = await asyncio.to_thread(
            probe_media_bytes,
            data,
            suffix=".png",
            temp_root=Path(tempfile.gettempdir()) / "takegraph",
        )
        if probe.width is None or probe.height is None:
            raise InvalidSourceError("Local image has no dimensions.")
        return probe.width, probe.height

    @staticmethod
    def _prepared(
        attempt: Attempt,
        project: Project,
        build: Build,
        node: BuildNode,
        model: str,
        inputs: tuple[bytes, ...],
        *,
        done: bool = False,
    ) -> PreparedLocalImageWork:
        return PreparedLocalImageWork(
            attempt_id=attempt.id,
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            build_node_id=node.id,
            stable_key=node.stable_key,
            model=model,
            inputs=inputs,
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


__all__ = ["LocalImageWorkHandlers", "PreparedLocalImageWork"]
