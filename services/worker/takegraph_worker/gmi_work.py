"""Durable execution for the four ORBIT keyframes and four video clips."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from typing import cast

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
    ProviderPolicy,
    Source,
    SourceVersion,
    Validation,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.canonical import JsonValue
from takegraph_domain.enums import (
    AttemptMechanism,
    AttemptStatus,
    BuildNodeStatus,
    BuildStatus,
    NodeType,
)
from takegraph_domain.errors import (
    AssetVerificationError,
    FeatureNotConfiguredError,
    InvalidSourceError,
    NotFoundError,
)
from takegraph_domain.execution.idempotency import submission_idempotency_key
from takegraph_domain.generation import (
    DurableGenerationAsset,
    GenerationEvent,
    GenerationEventKind,
    GenerationGateway,
    GenerationInput,
    GenerationRequest,
)
from takegraph_infrastructure.b2 import B2Store
from takegraph_infrastructure.media import MediaProbe, probe_media_bytes

from takegraph_worker.build_work import resolve_provider_policy, schedule_ready_nodes

KEYFRAME_KEYS = frozenset(f"image.keyframe.{index:02d}" for index in range(1, 5))
CLIP_KEYS = frozenset(f"video.clip.{index:02d}" for index in range(1, 5))
GMI_KEYS = KEYFRAME_KEYS | CLIP_KEYS


@dataclass(frozen=True, slots=True)
class PreparedGMIWork:
    request: GenerationRequest
    build_id: uuid.UUID
    project_id: uuid.UUID
    media_kind: str
    done: bool = False


class GMIWorkHandlers:
    """Translate persisted graph nodes into the provider-neutral GMI boundary."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        gateway: GenerationGateway,
        *,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._gateway = gateway
        self._environment = dict(os.environ if environment is None else environment)

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        prepared = await self._prepare(build_node_id)
        if prepared.done:
            return
        durable: DurableGenerationAsset | None = None
        completed = False
        failed = False
        try:
            async for event in self._gateway.execute(prepared.request):
                await self._persist_event(prepared, event)
                if event.kind is GenerationEventKind.STORED:
                    if event.asset is None:
                        raise AssetVerificationError("GMI stored event omitted its asset.")
                    if durable is not None:
                        raise AssetVerificationError(
                            "ORBIT generation returned more than one primary output."
                        )
                    durable = event.asset
                elif event.kind is GenerationEventKind.COMPLETED:
                    completed = True
                elif event.kind is GenerationEventKind.FAILED:
                    failed = True
                    await self._fail_event(prepared, event)
                    return
        except Exception as exc:
            if not failed:
                await self._fail_exception(prepared, exc)
            return
        if not completed or durable is None:
            await self._fail_exception(
                prepared, AssetVerificationError("GMI stream ended before durable completion.")
            )
            return
        await self._finalize(prepared, durable)

    async def _prepare(self, build_node_id: uuid.UUID) -> PreparedGMIWork:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("GMI build node was not found.")
            if node.stable_key not in GMI_KEYS:
                raise FeatureNotConfiguredError(f"GMI handler cannot execute {node.stable_key}.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("GMI build was not found.")
            project = await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if project is None or graph_node is None:
                raise InvalidSourceError("GMI node references incomplete graph data.")
            policy = await session.get(ProviderPolicy, graph_node.provider_policy_id)
            provider, model, timeout = resolve_provider_policy(policy, self._environment)
            if provider != "gmicloud":
                raise FeatureNotConfiguredError(f"{node.stable_key} requires GMI Cloud.")
            attempt = await session.scalar(
                select(Attempt)
                .where(Attempt.build_node_id == node.id)
                .order_by(Attempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is not None and attempt.status == str(AttemptStatus.SUCCEEDED):
                return await self._prepared(
                    build, project, node, attempt, graph_node, policy, (), model, timeout, True
                )
            if attempt is not None:
                raise InvalidSourceError(
                    f"GMI attempt cannot be submitted again from {attempt.status}; reconcile it."
                )
            if node.status != str(BuildNodeStatus.QUEUED):
                raise InvalidSourceError(f"GMI node is not runnable from {node.status}.")
            if build.status not in {str(BuildStatus.QUEUED), str(BuildStatus.RUNNING)}:
                raise InvalidSourceError(f"GMI build is not runnable from {build.status}.")
            resume_request_id = await self._recoverable_provider_request(
                session, build, node, model
            )
            input_assets = await self._input_assets(session, build, project, node.stable_key)
            for asset in input_assets:
                if asset.verified_at is None or not await asyncio.to_thread(
                    self._store.verify, asset.b2_key, expected_sha256=asset.sha256
                ):
                    raise InvalidSourceError(
                        f"GMI input {asset.id} failed stored-byte verification."
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
            return await self._prepared(
                build,
                project,
                node,
                attempt,
                graph_node,
                policy,
                input_assets,
                model,
                timeout,
                resume_provider_request_id=resume_request_id,
            )

    async def _prepared(
        self,
        build: Build,
        project: Project,
        node: BuildNode,
        attempt: Attempt,
        graph_node: GraphNode,
        policy: ProviderPolicy | None,
        assets: tuple[Asset, ...],
        model: str,
        timeout_seconds: int,
        done: bool = False,
        resume_provider_request_id: str | None = None,
    ) -> PreparedGMIWork:
        operation = graph_node.spec_json.get("normalized_operation")
        if not isinstance(operation, dict):
            raise InvalidSourceError("GMI normalized operation is malformed.")
        parameters = operation.get("parameters", {})
        if not isinstance(parameters, dict):
            raise InvalidSourceError("GMI parameters are malformed.")
        request = GenerationRequest(
            organization_id=project.organization_id,
            project_id=project.id,
            build_node_id=node.id,
            attempt_id=attempt.id,
            stable_key=node.stable_key,
            node_type=NodeType(graph_node.node_type),
            provider="gmicloud",
            model=model,
            prompt=await self._prompt(operation, assets),
            parameters=cast("dict[str, JsonValue]", parameters),
            inputs=tuple(
                GenerationInput(
                    asset_id=asset.id,
                    url=self._store.presign_get(
                        asset.b2_key, ttl_seconds=min(timeout_seconds + 120, 900)
                    ),
                    media_type=asset.mime_type,
                    sha256=asset.sha256,
                    size_bytes=asset.size_bytes,
                )
                for asset in assets
                if asset.media_kind == "IMAGE"
            ),
            fallback_models=self._fallback_models(policy),
            idempotency_key=attempt.idempotency_key,
            timeout_seconds=timeout_seconds,
            max_retries=self._max_retries(policy),
            resume_provider_request_id=resume_provider_request_id,
        )
        return PreparedGMIWork(
            request=request,
            build_id=build.id,
            project_id=project.id,
            media_kind="IMAGE" if node.stable_key in KEYFRAME_KEYS else "VIDEO",
            done=done,
        )

    async def _prompt(self, operation: dict[str, object], assets: tuple[Asset, ...]) -> str:
        template = operation.get("prompt_template")
        shot_index = operation.get("shot_index")
        if not isinstance(template, str) or not isinstance(shot_index, int):
            raise InvalidSourceError("GMI prompt template or shot index is malformed.")
        plan_asset = next(
            (asset for asset in assets if asset.mime_type == "application/json"), None
        )
        plan_note = ""
        if plan_asset is not None:
            try:
                plan_data = await asyncio.to_thread(self._store.get_bytes, plan_asset.b2_key)
                payload = json.loads(plan_data)
                shots = payload.get("shots") if isinstance(payload, dict) else None
                if not isinstance(shots, list):
                    raise ValueError
                shot = next(
                    (
                        item
                        for item in shots
                        if isinstance(item, dict) and item.get("index") == shot_index
                    ),
                    None,
                )
                if shot is None:
                    raise ValueError
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise InvalidSourceError(
                    f"Verified shot plan does not contain shot {shot_index}."
                ) from exc
            shot_json = json.dumps(shot, sort_keys=True, separators=(",", ":"))
            plan_note = f" Follow this exact verified shot specification: {shot_json}."
        return template.replace("{{ shot.index }}", str(shot_index)) + plan_note

    def _fallback_models(self, policy: ProviderPolicy | None) -> tuple[str, ...]:
        if policy is None:
            return ()
        primary = policy.definition_json.get("primary", {})
        raw = primary.get("same_provider_fallback_models", []) if isinstance(primary, dict) else []
        if not isinstance(raw, list):
            raise InvalidSourceError("GMI fallback model policy is malformed.")
        models: list[str] = []
        for value in raw:
            if not isinstance(value, str):
                raise InvalidSourceError("GMI fallback model must be a string.")
            if value.startswith("${") and value.endswith("}"):
                variable = value[2:-1]
                value = self._environment.get(variable, "")
                if not value:
                    raise FeatureNotConfiguredError(f"Fallback model requires {variable}.")
            models.append(value)
        return tuple(models)

    @staticmethod
    def _max_retries(policy: ProviderPolicy | None) -> int:
        retry = {} if policy is None else policy.definition_json.get("retry", {})
        value = retry.get("max_transient_retries", 0) if isinstance(retry, dict) else 0
        if not isinstance(value, int) or not 0 <= value <= 5:
            raise InvalidSourceError("GMI retry policy is malformed.")
        return value

    @staticmethod
    async def _recoverable_provider_request(
        session: AsyncSession, build: Build, node: BuildNode, model: str
    ) -> str | None:
        if build.parent_build_id is None:
            return None
        previous = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.parent_build_id,
                BuildNode.stable_key == node.stable_key,
                BuildNode.fingerprint == node.fingerprint,
                BuildNode.status == str(BuildNodeStatus.FAILED),
            )
        )
        if previous is None:
            return None
        return await session.scalar(
            select(AttemptEvent.provider_event_json["provider_request_id"].astext)
            .join(Attempt, Attempt.id == AttemptEvent.attempt_id)
            .where(
                Attempt.build_node_id == previous.id,
                Attempt.model == model,
                Attempt.status == str(AttemptStatus.FAILED),
                AttemptEvent.provider_event_json["provider_request_id"].astext.isnot(None),
            )
            .order_by(AttemptEvent.sequence.desc())
            .limit(1)
        )

    async def _input_assets(
        self, session: AsyncSession, build: Build, project: Project, stable_key: str
    ) -> tuple[Asset, ...]:
        plan = await self._selected_asset(session, build, "plan.shots", role="plan")
        if stable_key in KEYFRAME_KEYS:
            product = await self._product_asset(session, build, project)
            cutout = await self._selected_asset(session, build, "transform.product_cutout")
            return product, cutout, plan
        index = stable_key.rsplit(".", 1)[1]
        keyframe = await self._selected_asset(session, build, f"image.keyframe.{index}")
        return keyframe, plan

    @staticmethod
    async def _selected_asset(
        session: AsyncSession, build: Build, stable_key: str, *, role: str = "primary"
    ) -> Asset:
        node = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.id, BuildNode.stable_key == stable_key
            )
        )
        if node is None or node.selected_attempt_id is None:
            raise InvalidSourceError(f"GMI requires selected {stable_key} output.")
        asset = await session.scalar(
            select(Asset)
            .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
            .where(
                AttemptAsset.attempt_id == node.selected_attempt_id,
                AttemptAsset.role == role,
                AttemptAsset.selected.is_(True),
            )
            .order_by(AttemptAsset.ordinal)
            .limit(1)
        )
        if asset is None:
            raise InvalidSourceError(f"GMI selected {stable_key} asset is missing.")
        return asset

    @staticmethod
    async def _product_asset(session: AsyncSession, build: Build, project: Project) -> Asset:
        node = await session.scalar(
            select(BuildNode).where(
                BuildNode.build_id == build.id,
                BuildNode.stable_key == "source.product_reference",
            )
        )
        if node is None or node.selected_asset_set_hash is None:
            raise InvalidSourceError("GMI requires the selected product reference.")
        asset = await session.scalar(
            select(Asset)
            .join(SourceVersion, SourceVersion.asset_id == Asset.id)
            .join(Source, Source.id == SourceVersion.source_id)
            .where(
                Source.project_id == project.id,
                Source.stable_key == "source.product_reference",
                SourceVersion.content_hash == node.selected_asset_set_hash,
            )
            .order_by(SourceVersion.created_at.desc())
            .limit(1)
        )
        if asset is None:
            raise InvalidSourceError("GMI product reference is not indexed.")
        return asset

    async def _persist_event(self, prepared: PreparedGMIWork, event: GenerationEvent) -> None:
        async with self._session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == prepared.request.attempt_id).with_for_update()
            )
            if attempt is None:
                raise NotFoundError("GMI attempt disappeared.")
            if event.run_id:
                attempt.genblaze_run_id = event.run_id
            if event.kind is GenerationEventKind.PROVIDER_SUBMITTED:
                if attempt.status == str(AttemptStatus.SUBMITTING):
                    assert_transition(
                        AttemptStatus.SUBMITTING, AttemptStatus.SUBMITTED, subject="attempt"
                    )
                    attempt.status = str(AttemptStatus.SUBMITTED)
                    attempt.submitted_at = datetime.now(UTC)
            elif event.kind in {
                GenerationEventKind.PROVIDER_PROGRESS,
                GenerationEventKind.PROVIDER_RETRY,
                GenerationEventKind.PROVIDER_COMPLETED,
            }:
                if attempt.status == str(AttemptStatus.SUBMITTING):
                    assert_transition(
                        AttemptStatus.SUBMITTING, AttemptStatus.SUBMITTED, subject="attempt"
                    )
                    attempt.status = str(AttemptStatus.SUBMITTED)
                    attempt.submitted_at = datetime.now(UTC)
                if attempt.status == str(AttemptStatus.SUBMITTED):
                    assert_transition(
                        AttemptStatus.SUBMITTED, AttemptStatus.POLLING, subject="attempt"
                    )
                    attempt.status = str(AttemptStatus.POLLING)
            elif event.kind is GenerationEventKind.STORED:
                if attempt.status == str(AttemptStatus.SUBMITTING):
                    attempt.status = str(AttemptStatus.SUBMITTED)
                    attempt.submitted_at = datetime.now(UTC)
                if attempt.status == str(AttemptStatus.SUBMITTED):
                    attempt.status = str(AttemptStatus.POLLING)
                if attempt.status == str(AttemptStatus.POLLING):
                    assert_transition(
                        AttemptStatus.POLLING, AttemptStatus.FETCHING, subject="attempt"
                    )
                    attempt.status = str(AttemptStatus.FETCHING)
            self._attempt_event(
                session,
                attempt.id,
                f"gmi.{event.kind.value.lower()}",
                event.model_dump(mode="json", exclude={"attempt_id"}),
            )
            await session.commit()

    async def _finalize(self, prepared: PreparedGMIWork, durable: DurableGenerationAsset) -> None:
        key = self._store.key_from_url(durable.durable_url)
        if key is None:
            raise AssetVerificationError("GMI durable URL does not belong to configured B2.")
        if not await asyncio.to_thread(self._store.verify, key, expected_sha256=durable.sha256):
            raise AssetVerificationError("GMI output failed independent B2 verification.")
        raw = await asyncio.to_thread(self._store.get_bytes, key)
        if len(raw) != durable.size_bytes:
            raise AssetVerificationError("GMI durable output size does not match stored bytes.")
        probe = await asyncio.to_thread(
            probe_media_bytes,
            raw,
            suffix=self._suffix(durable.media_type),
            temp_root=Path(gettempdir()) / "takegraph",
        )
        self._validate_probe(prepared, probe)
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            if attempt.status == str(AttemptStatus.SUCCEEDED):
                return
            if attempt.status != str(AttemptStatus.FETCHING):
                raise InvalidSourceError(f"GMI completion cannot finalize from {attempt.status}.")
            assert_transition(AttemptStatus.FETCHING, AttemptStatus.STORED, subject="attempt")
            attempt.status = str(AttemptStatus.STORED)
            asset_id = await session.scalar(
                insert(Asset)
                .values(
                    id=uuid.uuid4(),
                    organization_id=project.organization_id,
                    sha256=durable.sha256,
                    size_bytes=durable.size_bytes,
                    mime_type=durable.media_type,
                    media_kind=prepared.media_kind,
                    b2_bucket=self._store.bucket,
                    b2_key=key,
                    metadata_json={
                        "stable_key": node.stable_key,
                        "width": probe.width,
                        "height": probe.height,
                        "duration_ms": probe.duration_ms,
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
                        Asset.sha256 == durable.sha256,
                    )
                )
            if asset_id is None:
                raise InvalidSourceError("GMI durable asset could not be indexed.")
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
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if graph_node is None:
                raise InvalidSourceError("GMI graph node disappeared.")
            validations = (
                ("storage_hash", {"sha256": durable.sha256}),
                ("media_integrity", self._probe_evidence(probe)),
                ("durable_manifest", {"genblaze_run_id": attempt.genblaze_run_id}),
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
            node.selected_asset_set_hash = durable.sha256
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

    async def _fail_event(self, prepared: PreparedGMIWork, event: GenerationEvent) -> None:
        await self._fail(
            prepared,
            error_class=str(event.error_class) if event.error_class else "INTERNAL",
            error_code=event.error_code or "GMI_FAILED",
            message=event.message or "GMI generation failed.",
        )

    async def _fail_exception(self, prepared: PreparedGMIWork, exc: Exception) -> None:
        await self._fail(
            prepared,
            error_class="INTERNAL",
            error_code=type(exc).__name__,
            message=str(exc)[:500] or "GMI execution failed.",
        )

    async def _fail(
        self,
        prepared: PreparedGMIWork,
        *,
        error_class: str,
        error_code: str,
        message: str,
    ) -> None:
        async with self._session_factory() as session:
            attempt, node, build, project = await self._locked(session, prepared)
            if attempt.status == str(AttemptStatus.SUCCEEDED):
                return
            assert_transition(
                AttemptStatus(attempt.status), AttemptStatus.FAILED, subject="attempt"
            )
            attempt.status = str(AttemptStatus.FAILED)
            attempt.error_class = error_class
            attempt.error_code = error_code[:64]
            attempt.error_message = message[:500]
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
        self, session: AsyncSession, prepared: PreparedGMIWork
    ) -> tuple[Attempt, BuildNode, Build, Project]:
        attempt = await session.scalar(
            select(Attempt).where(Attempt.id == prepared.request.attempt_id).with_for_update()
        )
        node = await session.scalar(
            select(BuildNode)
            .where(BuildNode.id == prepared.request.build_node_id)
            .with_for_update()
        )
        build = await session.scalar(
            select(Build).where(Build.id == prepared.build_id).with_for_update()
        )
        project = await session.get(Project, prepared.project_id)
        if attempt is None or node is None or build is None or project is None:
            raise NotFoundError("GMI execution state disappeared.")
        return attempt, node, build, project

    @staticmethod
    def _suffix(media_type: str) -> str:
        return {"image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4"}.get(
            media_type, ".bin"
        )

    @staticmethod
    def _validate_probe(prepared: PreparedGMIWork, probe: MediaProbe) -> None:
        if probe.width is None or probe.height is None:
            raise AssetVerificationError("Generated media has no video/image dimensions.")
        if prepared.media_kind == "VIDEO" and (probe.duration_ms is None or probe.duration_ms <= 0):
            raise AssetVerificationError("Generated video has no positive duration.")

    @staticmethod
    def _probe_evidence(probe: MediaProbe) -> dict[str, object]:
        return {
            "format": probe.format_name,
            "width": probe.width,
            "height": probe.height,
            "duration_ms": probe.duration_ms,
        }

    @staticmethod
    def _attempt_event(
        session: AsyncSession,
        attempt_id: uuid.UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        # JSON roundtrip strips enum instances without persisting SDK objects.
        normalized = json.loads(json.dumps(payload, default=str))
        session.add(
            AttemptEvent(
                attempt_id=attempt_id,
                provider_event_type=event_type,
                provider_event_json=normalized,
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


__all__ = ["CLIP_KEYS", "GMI_KEYS", "GMIWorkHandlers", "KEYFRAME_KEYS", "PreparedGMIWork"]
