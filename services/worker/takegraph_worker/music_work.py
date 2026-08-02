"""Crash-safe execution for the ORBIT music node."""

from __future__ import annotations

import asyncio
import json
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
    ProjectRevision,
    ProviderPolicy,
    Validation,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus, BuildStatus
from takegraph_domain.errors import (
    AssetVerificationError,
    FeatureNotConfiguredError,
    InvalidSourceError,
    NotFoundError,
    ProviderAuthError,
    ProviderQuotaError,
)
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.graph.orbit import DEFAULT_BRIEF_TEXT, PARAM_BRIEF_TEXT
from takegraph_infrastructure.b2 import B2Store
from takegraph_infrastructure.media import MediaProbe, probe_media_bytes

from takegraph_worker.build_work import resolve_provider_policy, schedule_ready_nodes
from takegraph_worker.elevenlabs_music_gateway import (
    ElevenLabsMusicGateway,
    MusicGenerationRequest,
    MusicGenerationResult,
    MusicGenerator,
)

MusicProber = Callable[[bytes], MediaProbe]


@dataclass(frozen=True, slots=True)
class PreparedMusicWork:
    action: str
    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    build_node_id: uuid.UUID
    model: str
    prompt: str
    duration_ms: int
    timeout_seconds: int
    idempotency_key: str
    recovered_result: MusicGenerationResult | None = None


class MusicWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        *,
        generator: MusicGenerator | None = None,
        prober: MusicProber | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._environment = dict(os.environ if environment is None else environment)
        self._generator = generator
        self._prober = prober or self._probe

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare(build_node_id)
        if prepared.action in {"DONE", "REVIEW"}:
            return
        result = prepared.recovered_result
        if result is None:
            generator = self._generator or ElevenLabsMusicGateway.from_env(self._environment)
            try:
                result = await generator.generate(
                    MusicGenerationRequest(
                        organization_id=prepared.organization_id,
                        project_id=prepared.project_id,
                        build_node_id=prepared.build_node_id,
                        attempt_id=prepared.attempt_id,
                        prompt=prepared.prompt,
                        model=prepared.model,
                        duration_ms=prepared.duration_ms,
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
                # Music has no provider idempotency or request ID on the live
                # account. Any interrupted synchronous submission is ambiguous.
                await self._ambiguous_submission(prepared, exc)
                return
            await self._persist_provider_result(prepared, result)

        if not await asyncio.to_thread(
            self._store.verify, result.b2_key, expected_sha256=result.sha256
        ):
            # The FETCHING event is already durable, so the queue may safely
            # retry this storage read without another provider submission.
            raise AssetVerificationError("Stored music failed independent B2 re-verification.")
        music_bytes = await asyncio.to_thread(self._store.get_bytes, result.b2_key)
        try:
            probe = await asyncio.to_thread(self._prober, music_bytes)
            if probe.media_kind != "AUDIO":
                raise InvalidSourceError("Music output does not contain a decodable audio stream.")
        except InvalidSourceError as exc:
            await self._terminal_failure(prepared, exc)
            return
        await self._finalize(prepared, result, probe)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedMusicWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Music build node was not found.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Music build was not found.")
            project = await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            revision = await session.get(ProjectRevision, build.project_revision_id)
            if project is None or graph_node is None or revision is None:
                raise InvalidSourceError("Music execution references incomplete build data.")
            if node.stable_key != "audio.music":
                raise FeatureNotConfiguredError(f"Music handler cannot execute {node.stable_key}.")
            policy = await session.get(ProviderPolicy, graph_node.provider_policy_id)
            provider, model, timeout = resolve_provider_policy(policy, self._environment)
            if provider != "elevenlabs":
                raise FeatureNotConfiguredError("audio.music requires ElevenLabs.")
            prompt, duration_ms = await self._prompt(session, build, graph_node, revision)

            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None:
                if attempt.status == str(AttemptStatus.SUCCEEDED):
                    return self._prepared(
                        "DONE", attempt, project, build, node, model, prompt, duration_ms, timeout
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
                        prompt,
                        duration_ms,
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
                        prompt,
                        duration_ms,
                        timeout,
                    )
                raise InvalidSourceError(
                    f"Existing music attempt is in unsupported state {attempt.status}."
                )

            if node.status != str(BuildNodeStatus.QUEUED):
                raise InvalidSourceError(f"Music node is not runnable from {node.status}.")
            if build.status not in {str(BuildStatus.QUEUED), str(BuildStatus.RUNNING)}:
                raise InvalidSourceError(f"Music build is not runnable from {build.status}.")
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
            return self._prepared(
                "GENERATE",
                attempt,
                project,
                build,
                node,
                model,
                prompt,
                duration_ms,
                timeout,
            )

    async def _prompt(
        self,
        session: AsyncSession,
        build: Build,
        graph_node: GraphNode,
        revision: ProjectRevision,
    ) -> tuple[str, int]:
        plan_node = await session.scalar(
            select(BuildNode)
            .join(GraphEdge, GraphEdge.from_node_id == BuildNode.graph_node_id)
            .where(
                BuildNode.build_id == build.id,
                GraphEdge.to_node_id == graph_node.id,
                BuildNode.stable_key == "plan.shots",
            )
        )
        if plan_node is None or plan_node.selected_attempt_id is None:
            raise InvalidSourceError("Music requires the selected shot-plan attempt.")
        plan_asset = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(
                AttemptAsset.attempt_id == plan_node.selected_attempt_id,
                AttemptAsset.role == "plan",
                AttemptAsset.selected.is_(True),
            )
        )
        if plan_asset is None or plan_asset.verified_at is None:
            raise InvalidSourceError("Music shot plan is not a verified durable asset.")
        if not await asyncio.to_thread(
            self._store.verify, plan_asset.b2_key, expected_sha256=plan_asset.sha256
        ):
            raise InvalidSourceError("Music shot plan failed stored-byte verification.")
        plan_bytes = await asyncio.to_thread(self._store.get_bytes, plan_asset.b2_key)
        try:
            plan = json.loads(plan_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidSourceError("Music shot plan is not valid JSON.") from exc
        parameters = revision.spec_json.get("parameters", {})
        brief = parameters.get(PARAM_BRIEF_TEXT, DEFAULT_BRIEF_TEXT)
        if not isinstance(brief, str):
            raise InvalidSourceError("Music creative brief must be a string.")
        operation = graph_node.spec_json.get("normalized_operation", {})
        if not isinstance(operation, dict):
            raise InvalidSourceError("Music operation is malformed.")
        template = operation.get("prompt_template")
        operation_parameters = operation.get("parameters", {})
        if not isinstance(template, str) or not isinstance(operation_parameters, dict):
            raise InvalidSourceError("Music prompt configuration is malformed.")
        duration = operation_parameters.get("duration_seconds")
        if not isinstance(duration, int) or duration < 3 or duration > 600:
            raise InvalidSourceError("Music duration is outside the provider limit.")
        plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        prompt = (
            f"{template}\nCreative brief: {brief}\nShot plan: {plan_json}\n"
            "Instrumental only. No vocals, spoken words, or product claims."
        )
        if len(prompt) > 5_000:
            raise InvalidSourceError("Music prompt exceeds the provider safety limit.")
        return prompt, duration * 1_000

    async def _persist_provider_result(
        self, prepared: PreparedMusicWork, result: MusicGenerationResult
    ) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("Music attempt disappeared after provider completion.")
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
        prepared: PreparedMusicWork,
        result: MusicGenerationResult,
        probe: MediaProbe,
    ) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked_execution(session, prepared)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("Music validation policy is missing.")
            if attempt.status == str(AttemptStatus.SUCCEEDED):
                return
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.STORED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.STORED)
            self._attempt_event(session, attempt.id, "attempt.stored", {"sha256": result.sha256})
            asset_id = await session.scalar(
                insert(Asset)
                .values(
                    id=uuid.uuid4(),
                    organization_id=project.organization_id,
                    sha256=result.sha256,
                    size_bytes=result.size_bytes,
                    mime_type=result.media_type,
                    media_kind="AUDIO",
                    b2_bucket=self._store.bucket,
                    b2_key=result.b2_key,
                    metadata_json={
                        "stable_key": node.stable_key,
                        "role": "music",
                        "model": result.model,
                        "provider_request_id": result.provider_request_id,
                        "duration_ms": probe.duration_ms,
                        "sample_rate": probe.sample_rate,
                        "channels": probe.channels,
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
                        Asset.sha256 == result.sha256,
                    )
                )
            if asset_id is None:
                raise InvalidSourceError("Music asset could not be indexed.")
            await session.execute(
                insert(AttemptAsset)
                .values(
                    id=uuid.uuid4(),
                    attempt_id=attempt.id,
                    asset_id=asset_id,
                    role="music",
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
                ("storage_hash", {"sha256": result.sha256}),
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
            node.selected_asset_set_hash = result.sha256
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

    async def _terminal_failure(self, prepared: PreparedMusicWork, exc: Exception) -> None:
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

    async def _ambiguous_submission(self, prepared: PreparedMusicWork, exc: Exception) -> None:
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
        attempt.error_message = "ElevenLabs Music submission is ambiguous; review required."
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
    ) -> MusicGenerationResult:
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
            raise InvalidSourceError("FETCHING music attempt has no persisted output.")
        return MusicGenerationResult.model_validate(event.provider_event_json)

    async def _locked_execution(
        self, session: AsyncSession, prepared: PreparedMusicWork
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
            raise NotFoundError("Music execution state disappeared.")
        return attempt, node, build, project

    @staticmethod
    def _prepared(
        action: str,
        attempt: Attempt,
        project: Project,
        build: Build,
        node: BuildNode,
        model: str,
        prompt: str,
        duration_ms: int,
        timeout: int,
        recovered: MusicGenerationResult | None = None,
    ) -> PreparedMusicWork:
        return PreparedMusicWork(
            action=action,
            attempt_id=attempt.id,
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            build_node_id=node.id,
            model=model,
            prompt=prompt,
            duration_ms=duration_ms,
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

    def _probe(self, data: bytes) -> MediaProbe:
        configured = self._environment.get("WORK_TEMP_ROOT")
        temp_root = Path(configured) if configured else Path(tempfile.gettempdir()) / "takegraph"
        return probe_media_bytes(data, suffix=".mp3", temp_root=temp_root)


__all__ = ["MusicWorkHandlers", "PreparedMusicWork"]
