"""Crash-safe local execution for ``compose.delivery_package``."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tempfile import gettempdir
from typing import Protocol

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
    Validation,
)
from takegraph_domain.builds.asset_set import SelectedAsset, selected_asset_set_hash
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_domain.errors import FeatureNotConfiguredError, InvalidSourceError, NotFoundError
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.storage.keys import content_address
from takegraph_infrastructure.b2 import B2Store, StoredObject
from takegraph_infrastructure.delivery import (
    DeliveryArtifact,
    DeliveryInput,
    compose_delivery_package,
)

from takegraph_worker.anthropic_gateway import CopyPack
from takegraph_worker.build_work import schedule_ready_nodes


class DeliveryComposer(Protocol):
    def __call__(
        self, source: DeliveryInput, *, temp_root: Path
    ) -> tuple[DeliveryArtifact, ...]: ...


@dataclass(frozen=True, slots=True)
class PreparedDeliveryWork:
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    source: DeliveryInput
    done: bool = False


class DeliveryWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        *,
        composer: DeliveryComposer = compose_delivery_package,
        temp_root: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._composer = composer
        self._temp_root = (temp_root or Path(gettempdir()) / "takegraph").resolve()

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare(build_node_id)
        if prepared.done:
            return
        try:
            artifacts = await asyncio.to_thread(
                partial(self._composer, prepared.source, temp_root=self._temp_root)
            )
            if {artifact.role for artifact in artifacts} != {
                "master_16x9",
                "master_9x16",
                "final_audio",
                "thumbnail_16x9",
                "thumbnail_9x16",
                "captions",
                "report",
            }:
                raise InvalidSourceError("Delivery composer returned an incomplete artifact set.")
            stored = await self._store_artifacts(prepared, artifacts)
            await self._persist_fetching(prepared)
            await self._finalize(prepared, stored)
        except (InvalidSourceError, ValueError, OSError) as exc:
            await self._fail(prepared, exc)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedDeliveryWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Delivery build node was not found.")
            if node.stable_key != "compose.delivery_package":
                raise FeatureNotConfiguredError(
                    f"Delivery handler cannot execute {node.stable_key}."
                )
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Delivery build was not found.")
            project = await session.get(Project, build.project_id)
            if project is None:
                raise InvalidSourceError("Delivery build has no project.")
            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None and attempt.status == str(AttemptStatus.SUCCEEDED):
                return PreparedDeliveryWork(
                    attempt.id,
                    project.organization_id,
                    project.id,
                    build.id,
                    node.id,
                    DeliveryInput((b"", b"", b"", b""), b"", b"", b"", (), ""),
                    done=True,
                )
            if attempt is not None:
                raise InvalidSourceError(f"Delivery attempt cannot be rerun from {attempt.status}.")
            if node.status != str(BuildNodeStatus.QUEUED):
                raise InvalidSourceError(f"Delivery node is not runnable from {node.status}.")
            assets = {
                key: await self._selected_asset(session, build, key)
                for key in (
                    "video.clip.01",
                    "video.clip.02",
                    "video.clip.03",
                    "video.clip.04",
                    "audio.narration",
                    "audio.music",
                    "graphic.end_card",
                    "copy.pack",
                )
            }
            for asset in assets.values():
                if asset.verified_at is None or not await asyncio.to_thread(
                    self._store.verify, asset.b2_key, expected_sha256=asset.sha256
                ):
                    raise InvalidSourceError(
                        f"Delivery input {asset.id} failed stored-byte verification."
                    )
            payloads = {
                key: await asyncio.to_thread(self._store.get_bytes, asset.b2_key)
                for key, asset in assets.items()
            }
            copy = CopyPack.model_validate_json(payloads["copy.pack"])
            source = DeliveryInput(
                clips=(
                    payloads["video.clip.01"],
                    payloads["video.clip.02"],
                    payloads["video.clip.03"],
                    payloads["video.clip.04"],
                ),
                narration=payloads["audio.narration"],
                music=payloads["audio.music"],
                end_card=payloads["graphic.end_card"],
                captions=copy.captions,
                legal_line=copy.legal_line,
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
                model="ffmpeg.delivery.v1",
                idempotency_key=submission_idempotency_key(
                    build_node_id=node.id,
                    fingerprint=node.fingerprint,
                    mechanism=AttemptMechanism.PRIMARY,
                    provider="local",
                    model="ffmpeg.delivery.v1",
                ),
                status=str(AttemptStatus.SUBMITTING),
            )
            session.add(attempt)
            self._attempt_event(session, attempt.id, "attempt.submitting", {})
            await session.commit()
            return PreparedDeliveryWork(
                attempt.id,
                project.organization_id,
                project.id,
                build.id,
                node.id,
                source,
            )

    @staticmethod
    async def _selected_asset(session: AsyncSession, build: Build, stable_key: str) -> Asset:
        node = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.id, BuildNode.stable_key == stable_key
            )
        )
        if node is None or node.selected_attempt_id is None:
            raise InvalidSourceError(f"Delivery requires selected {stable_key} output.")
        asset = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(
                AttemptAsset.attempt_id == node.selected_attempt_id,
                AttemptAsset.selected.is_(True),
            )
            .order_by(AttemptAsset.ordinal)
            .limit(1)
        )
        if asset is None:
            raise InvalidSourceError(f"Delivery selected {stable_key} asset is missing.")
        return asset

    async def _store_artifacts(
        self,
        prepared: PreparedDeliveryWork,
        artifacts: tuple[DeliveryArtifact, ...],
    ) -> tuple[tuple[DeliveryArtifact, StoredObject], ...]:
        results: list[tuple[DeliveryArtifact, StoredObject]] = []
        for artifact in artifacts:
            extension = artifact.filename.rsplit(".", 1)[-1]
            key = content_address(
                organization_id=prepared.organization_id,
                sha256=artifact.sha256,
                extension=extension,
                prefix=self._store.prefix,
            )
            stored = await asyncio.to_thread(
                self._store.store_bytes,
                key,
                artifact.data,
                content_type=artifact.mime_type,
                metadata={
                    "attempt_id": str(prepared.attempt_id),
                    "role": artifact.role,
                },
            )
            if not await asyncio.to_thread(self._store.verify, key, expected_sha256=stored.sha256):
                raise InvalidSourceError(
                    f"Delivery artifact {artifact.role} failed B2 re-verification."
                )
            results.append((artifact, stored))
        return tuple(results)

    async def _persist_fetching(self, prepared: PreparedDeliveryWork) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Delivery attempt disappeared before indexing.")
            for target in (
                AttemptStatus.SUBMITTED,
                AttemptStatus.POLLING,
                AttemptStatus.FETCHING,
            ):
                assert_transition(AttemptStatus(attempt.status), target, subject="attempt")
                attempt.status = str(target)
            attempt.submitted_at = datetime.now(UTC)
            self._attempt_event(session, attempt.id, "attempt.fetching", {})
            await session.commit()

    async def _finalize(
        self,
        prepared: PreparedDeliveryWork,
        outputs: tuple[tuple[DeliveryArtifact, StoredObject], ...],
    ) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("Delivery graph node disappeared.")
            assert_transition(AttemptStatus.FETCHING, AttemptStatus.STORED, subject="attempt")
            attempt.status = str(AttemptStatus.STORED)
            asset_ids: list[uuid.UUID] = []
            selected_hashes: dict[str, str] = {}
            selected: list[SelectedAsset] = []
            for ordinal, (artifact, stored) in enumerate(outputs):
                asset_id = await session.scalar(
                    insert(Asset)
                    .values(
                        id=uuid.uuid4(),
                        organization_id=project.organization_id,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        mime_type=artifact.mime_type,
                        media_kind=artifact.media_kind,
                        b2_bucket=self._store.bucket,
                        b2_key=stored.key,
                        storage_version_id=stored.version_id,
                        metadata_json={
                            "stable_key": node.stable_key,
                            "role": artifact.role,
                            "filename": artifact.filename,
                            **artifact.metadata,
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
                    raise InvalidSourceError("Delivery asset could not be indexed.")
                asset_ids.append(asset_id)
                selected_hashes[artifact.role] = stored.sha256
                selected.append(
                    SelectedAsset(role=artifact.role, ordinal=ordinal, sha256=stored.sha256)
                )
                await session.execute(
                    insert(AttemptAsset)
                    .values(
                        id=uuid.uuid4(),
                        attempt_id=attempt.id,
                        asset_id=asset_id,
                        role=artifact.role,
                        ordinal=ordinal,
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
            asset_set_hash = selected_asset_set_hash(selected)
            validation_ids: list[str] = []
            for gate_key, evidence in (
                ("artifact_set", {"roles": sorted(selected_hashes)}),
                ("storage_hashes", {"asset_set_hash": asset_set_hash}),
                ("ffmpeg_report", {"report_sha256": selected_hashes["report"]}),
            ):
                validation_id = uuid.uuid4()
                validation_ids.append(str(validation_id))
                session.add(
                    Validation(
                        id=validation_id,
                        build_node_id=node.id,
                        attempt_id=attempt.id,
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
            node.selected_asset_set_hash = asset_set_hash
            node.reuse_proof_json = {
                "validations_current": True,
                "validation_policy_id": str(graph_node.validation_policy_id),
                "validation_ids": validation_ids,
                "asset_ids": [str(asset_id) for asset_id in asset_ids],
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

    async def _fail(self, prepared: PreparedDeliveryWork, exc: Exception) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.FAILED)
            attempt.error_code = type(exc).__name__
            attempt.error_message = str(exc)[:500]
            attempt.completed_at = datetime.now(UTC)
            assert_transition(BuildNodeStatus(node.status), BuildNodeStatus.FAILED, subject="node")
            self._node_transition(session, project, build, node, BuildNodeStatus.FAILED)
            node.completed_at = datetime.now(UTC)
            if build.status == str(BuildStatus.RUNNING):
                assert_transition(BuildStatus.RUNNING, BuildStatus.FAILED, subject="build")
                self._build_transition(session, project, build, BuildStatus.FAILED)
                build.completed_at = datetime.now(UTC)
            await session.commit()

    async def _locked(
        self, session: AsyncSession, prepared: PreparedDeliveryWork
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
            raise NotFoundError("Delivery execution state disappeared.")
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


__all__ = ["DeliveryWorkHandlers", "PreparedDeliveryWork"]
