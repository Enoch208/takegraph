"""Crash-safe execution for the ORBIT narration node."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
    ProviderPolicy,
    Validation,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_domain.errors import (
    FeatureNotConfiguredError,
    InvalidSourceError,
    NotFoundError,
    ProviderAuthError,
    ProviderQuotaError,
)
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.storage.keys import content_address
from takegraph_infrastructure.b2 import B2Store, StoredObject
from takegraph_infrastructure.media import MediaProbe, normalize_narration_bytes, probe_media_bytes

from takegraph_worker.anthropic_gateway import CopyPack
from takegraph_worker.build_work import resolve_provider_policy, schedule_ready_nodes
from takegraph_worker.elevenlabs_gateway import (
    ElevenLabsNarrationGateway,
    NarrationGenerator,
    NarrationRequest,
    NarrationResult,
)

NarrationNormalizer = Callable[[bytes], tuple[bytes, MediaProbe]]


@dataclass(frozen=True, slots=True)
class PreparedNarrationWork:
    action: str
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    model: str
    text: str
    timeout_seconds: int
    idempotency_key: str
    recovered_result: NarrationResult | None = None


class NarrationWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        *,
        generator: NarrationGenerator | None = None,
        normalizer: NarrationNormalizer | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._environment = dict(os.environ if environment is None else environment)
        self._generator = generator
        self._normalizer = normalizer or self._normalize

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare(build_node_id)
        if prepared.action in {"DONE", "REVIEW"}:
            return
        result = prepared.recovered_result
        if result is None:
            generator = self._generator or ElevenLabsNarrationGateway.from_env(self._environment)
            try:
                result = await generator.generate(
                    NarrationRequest(
                        organization_id=prepared.organization_id,
                        project_id=prepared.project_id,
                        build_node_id=prepared.build_node_id,
                        attempt_id=prepared.attempt_id,
                        text=prepared.text,
                        model=prepared.model,
                        idempotency_key=prepared.idempotency_key,
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

        raw_key = self._store.key_from_url(result.asset.durable_url)
        if raw_key is None:
            raise InvalidSourceError("Narration manifest URL does not belong to the work bucket.")
        # One download, hashed on the way through — see B2Store.get_verified.
        raw_bytes = await asyncio.to_thread(
            self._store.get_verified,
            raw_key,
            expected_sha256=result.asset.sha256,
        )
        normalized, probe = await asyncio.to_thread(self._normalizer, raw_bytes)
        digest = hashlib.sha256(normalized).hexdigest()
        normalized_key = content_address(
            organization_id=prepared.organization_id,
            sha256=digest,
            extension="wav",
            prefix=self._store.prefix,
        )
        stored = await asyncio.to_thread(
            self._store.store_bytes,
            normalized_key,
            normalized,
            content_type="audio/wav",
            metadata={"attempt_id": str(prepared.attempt_id), "role": "narration"},
        )
        if not await asyncio.to_thread(
            self._store.verify, normalized_key, expected_sha256=stored.sha256
        ):
            raise InvalidSourceError("Normalized narration failed B2 re-verification.")
        await self._finalize(prepared, result, raw_key, stored, probe)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedNarrationWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Narration build node was not found.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Narration build was not found.")
            project = await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if project is None or graph_node is None:
                raise InvalidSourceError("Narration execution references incomplete graph data.")
            if node.stable_key != "audio.narration":
                raise FeatureNotConfiguredError(
                    f"Narration handler cannot execute {node.stable_key}."
                )
            policy = await session.get(ProviderPolicy, graph_node.provider_policy_id)
            provider, model, timeout = resolve_provider_policy(policy, self._environment)
            if provider != "elevenlabs":
                raise FeatureNotConfiguredError("audio.narration requires ElevenLabs.")
            copy_pack = await self._copy_pack(session, build, graph_node)
            text = copy_pack.narration

            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None:
                if attempt.status == str(AttemptStatus.SUCCEEDED):
                    return self._prepared(
                        "DONE", attempt, project, build, node, model, text, timeout
                    )
                if attempt.status == str(AttemptStatus.FETCHING):
                    recovered = await self._recover_result(session, attempt.id)
                    return self._prepared(
                        "STORE", attempt, project, build, node, model, text, timeout, recovered
                    )
                if attempt.status == str(AttemptStatus.SUBMITTING):
                    self._mark_ambiguous(session, project, build, node, attempt)
                    await session.commit()
                    return self._prepared(
                        "REVIEW", attempt, project, build, node, model, text, timeout
                    )
                raise InvalidSourceError(
                    f"Existing narration attempt is in unsupported state {attempt.status}."
                )

            if node.status != str(BuildNodeStatus.QUEUED):
                raise InvalidSourceError(f"Narration node is not runnable from {node.status}.")
            if build.status != str(BuildStatus.RUNNING):
                raise InvalidSourceError(f"Narration build is not runnable from {build.status}.")
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
            self._attempt_event(session, attempt.id, "attempt.submitting", {})
            await session.commit()
            return self._prepared("GENERATE", attempt, project, build, node, model, text, timeout)

    async def _copy_pack(
        self,
        session: AsyncSession,
        build: Build,
        graph_node: GraphNode,
    ) -> CopyPack:
        predecessor = await session.scalar(
            select(BuildNode)
            .join(GraphEdge, GraphEdge.from_node_id == BuildNode.graph_node_id)
            .where(
                BuildNode.build_id == build.id,
                GraphEdge.to_node_id == graph_node.id,
                BuildNode.stable_key == "copy.pack",
            )
        )
        if predecessor is None or predecessor.selected_attempt_id is None:
            raise InvalidSourceError("Narration requires the selected copy-pack attempt.")
        asset = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(
                AttemptAsset.attempt_id == predecessor.selected_attempt_id,
                AttemptAsset.role == "copy",
                AttemptAsset.selected.is_(True),
            )
        )
        if asset is None or asset.verified_at is None:
            raise InvalidSourceError("Narration copy input is not a verified durable asset.")
        data = await asyncio.to_thread(
            self._store.get_verified, asset.b2_key, expected_sha256=asset.sha256
        )
        try:
            return CopyPack.model_validate_json(data)
        except ValueError as exc:
            raise InvalidSourceError("Narration copy input does not match copy_pack.v1.") from exc

    async def _persist_provider_result(
        self, prepared: PreparedNarrationWork, result: NarrationResult
    ) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Narration attempt disappeared after provider completion.")
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
            attempt.genblaze_run_id = result.run_id
            attempt.submitted_at = datetime.now(UTC)
            await session.commit()

    async def _finalize(
        self,
        prepared: PreparedNarrationWork,
        result: NarrationResult,
        raw_key: str,
        normalized: StoredObject,
        probe: MediaProbe,
    ) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked_execution(session, prepared)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("Narration validation policy is missing.")
            if attempt.status == str(AttemptStatus.SUCCEEDED):
                return
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.STORED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.STORED)
            self._attempt_event(
                session, attempt.id, "attempt.stored", {"sha256": normalized.sha256}
            )

            raw_asset_id = await self._index_asset(
                session,
                organization_id=project.organization_id,
                sha256=result.asset.sha256,
                size_bytes=result.asset.size_bytes,
                mime_type=result.asset.media_type,
                b2_key=raw_key,
                metadata={"stable_key": node.stable_key, "role": "provider_raw"},
            )
            selected_asset_id = await self._index_asset(
                session,
                organization_id=project.organization_id,
                sha256=normalized.sha256,
                size_bytes=normalized.size_bytes,
                mime_type=normalized.content_type,
                b2_key=normalized.key,
                version_id=normalized.version_id,
                derived_from_asset_id=raw_asset_id,
                metadata={
                    "stable_key": node.stable_key,
                    "role": "narration",
                    "sample_rate": probe.sample_rate,
                    "channels": probe.channels,
                    "duration_ms": probe.duration_ms,
                },
            )
            for role, asset_id, selected in (
                ("provider_raw", raw_asset_id, False),
                ("narration", selected_asset_id, True),
            ):
                await session.execute(
                    insert(AttemptAsset)
                    .values(
                        id=uuid.uuid4(),
                        attempt_id=attempt.id,
                        asset_id=asset_id,
                        role=role,
                        ordinal=0,
                        selected=selected,
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
                ("storage_hash", {"sha256": normalized.sha256}),
                (
                    "media_integrity",
                    {
                        "media_kind": probe.media_kind,
                        "format_name": probe.format_name,
                        "duration_ms": probe.duration_ms,
                    },
                ),
                (
                    "audio_properties",
                    {"sample_rate": probe.sample_rate, "channels": probe.channels},
                ),
                ("manifest_integrity", {"manifest_hash": result.manifest_hash}),
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
                        asset_id=selected_asset_id,
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
            node.selected_asset_set_hash = normalized.sha256
            node.reuse_proof_json = {
                "validations_current": True,
                "validation_policy_id": str(graph_node.validation_policy_id),
                "validation_ids": validation_ids,
                "asset_ids": [str(selected_asset_id)],
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

    async def _index_asset(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        sha256: str,
        size_bytes: int,
        mime_type: str,
        b2_key: str,
        metadata: dict[str, object],
        version_id: str | None = None,
        derived_from_asset_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        asset_id = await session.scalar(
            insert(Asset)
            .values(
                id=uuid.uuid4(),
                organization_id=organization_id,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
                media_kind="AUDIO",
                b2_bucket=self._store.bucket,
                b2_key=b2_key,
                storage_version_id=version_id,
                metadata_json=metadata,
                derived_from_asset_id=derived_from_asset_id,
                verified_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[Asset.organization_id, Asset.sha256])
            .returning(Asset.id)
        )
        if asset_id is None:
            asset_id = await session.scalar(
                select(Asset.id).where(
                    Asset.organization_id == organization_id,
                    Asset.sha256 == sha256,
                )
            )
        if asset_id is None:
            raise InvalidSourceError("Narration asset could not be indexed.")
        return asset_id

    async def _terminal_failure(self, prepared: PreparedNarrationWork, exc: Exception) -> None:
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
            current = BuildStatus(build.status)
            assert_transition(current, BuildStatus.FAILED, subject="build")
            self._build_transition(session, project, build, BuildStatus.FAILED)
            build.completed_at = datetime.now(UTC)
            await session.commit()

    async def _ambiguous_submission(self, prepared: PreparedNarrationWork, exc: Exception) -> None:
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
        attempt.error_message = "ElevenLabs submission outcome is ambiguous; review required."
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

    async def _recover_result(
        self, session: AsyncSession, attempt_id: uuid.UUID
    ) -> NarrationResult:
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
            raise InvalidSourceError("FETCHING narration attempt has no persisted output.")
        return NarrationResult.model_validate(event.provider_event_json)

    async def _locked_execution(
        self, session: AsyncSession, prepared: PreparedNarrationWork
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
            raise NotFoundError("Narration execution state disappeared.")
        return attempt, node, build, project

    @staticmethod
    def _prepared(
        action: str,
        attempt: Attempt,
        project: Project,
        build: Build,
        node: BuildNode,
        model: str,
        text: str,
        timeout: int,
        recovered: NarrationResult | None = None,
    ) -> PreparedNarrationWork:
        return PreparedNarrationWork(
            action=action,
            attempt_id=attempt.id,
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            build_node_id=node.id,
            model=model,
            text=text,
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

    def _normalize(self, data: bytes) -> tuple[bytes, MediaProbe]:
        configured = self._environment.get("WORK_TEMP_ROOT")
        temp_root = Path(configured) if configured else Path(tempfile.gettempdir()) / "takegraph"
        normalized = normalize_narration_bytes(data, temp_root=temp_root)
        probe = probe_media_bytes(normalized, suffix=".wav", temp_root=temp_root)
        return normalized, probe


__all__ = ["NarrationWorkHandlers", "PreparedNarrationWork"]
