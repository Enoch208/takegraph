"""Create the real ORBIT baseline, publish v1, and persist landing proof events."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import unicodedata
import uuid
from pathlib import Path

import httpx
from sqlalchemy import func, select
from takegraph_api.changes import (
    ChangeImpactService,
    ChangeSetCreateRequest,
    RevisionParametersPatch,
    RevisionPatch,
)
from takegraph_api.db.models import (
    Asset,
    AttemptAsset,
    Build,
    BuildNode,
    DomainEvent,
    GraphNode,
    GraphRevision,
    Organization,
    Project,
    Release,
    Source,
    SourceVersion,
)
from takegraph_api.db.session import dispose_engine, get_session_factory
from takegraph_api.projects import ProjectCreateRequest, ProjectService
from takegraph_api.releases import ReleaseService
from takegraph_api.uploads import UploadInitiationRequest, upload_service
from takegraph_domain.auth import Principal
from takegraph_domain.enums import BuildNodeStatus, BuildStatus, Role
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.fingerprint import compute_source_fingerprint
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    DEFAULT_LEGAL_LINE,
    ORBIT_TEMPLATE,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
)
from takegraph_domain.graph.orbit_policies import orbit_policy_hashes
from takegraph_domain.graph.types import CompiledNode
from takegraph_infrastructure.b2 import B2Settings, B2Store
from takegraph_worker.build_work import schedule_ready_nodes
from takegraph_worker.gmi_gateway import GMICloudGateway, GMICloudSettings
from takegraph_worker.gmi_work import GMIWorkHandlers
from takegraph_worker.runtime import WorkerRuntime

DEMO_ORGANIZATION_SLUG = "takegraph-demo"
DEMO_PROJECT_SLUG = "orbit-hydration"
ACTOR_ID = uuid.UUID("a02c0ef8-9bcc-4e4c-a994-69e34bc9485e")


def _load_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, separators=(",", ":")), flush=True)


async def _ensure_project(product_path: Path, store: B2Store) -> tuple[Principal, Project, Asset]:
    factory = get_session_factory()
    raw = await asyncio.to_thread(product_path.read_bytes)
    digest = hashlib.sha256(raw).hexdigest()
    async with factory() as session:
        organization = await session.scalar(
            select(Organization).where(Organization.slug == DEMO_ORGANIZATION_SLUG)
        )
        if organization is None:
            organization = Organization(
                id=uuid.uuid4(),
                slug=DEMO_ORGANIZATION_SLUG,
                name="TAKEGRAPH Demo",
            )
            session.add(organization)
            await session.flush()
        principal = Principal(
            actor_id=ACTOR_ID,
            subject="orbit-seed-owner",
            organization_id=organization.id,
            role=Role.OWNER,
        )
        project = await session.scalar(
            select(Project).where(
                Project.organization_id == organization.id,
                Project.slug == DEMO_PROJECT_SLUG,
            )
        )
        if project is None:
            created = await ProjectService(session).create(
                principal=principal,
                request=ProjectCreateRequest(
                    slug=DEMO_PROJECT_SLUG,
                    name="ORBIT Hydration",
                    spec={
                        "parameters": {
                            PARAM_LEGAL_LINE: DEFAULT_LEGAL_LINE,
                            PARAM_BRIEF_TEXT: DEFAULT_BRIEF_TEXT,
                        }
                    },
                ),
            )
            project = await session.get(Project, created.id)
            if project is None:
                raise RuntimeError("Created ORBIT project disappeared.")
            project.is_demo = True
        existing_asset = await session.scalar(
            select(Asset)
            .join(SourceVersion, SourceVersion.asset_id == Asset.id)
            .join(Source, Source.id == SourceVersion.source_id)
            .where(
                Asset.organization_id == organization.id,
                Asset.sha256 == digest,
                Source.project_id == project.id,
                Source.stable_key == "source.product_reference",
            )
            .limit(1)
        )
        await session.commit()
        if existing_asset is not None:
            return principal, project, existing_asset

    async with factory() as session:
        project = await session.scalar(
            select(Project).where(
                Project.organization_id == principal.organization_id,
                Project.slug == DEMO_PROJECT_SLUG,
            )
        )
        if project is None:
            raise RuntimeError("ORBIT project is missing before upload.")
        service = upload_service(session, store)
        initiated = await service.initiate(
            project_id=project.id,
            principal=principal,
            request=UploadInitiationRequest(
                source_stable_key="source.product_reference",
                file_name=product_path.name,
                size_bytes=len(raw),
                mime_type="image/png",
                sha256=digest,
            ),
        )
        await session.commit()
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.put(
            initiated.upload_url,
            content=raw,
            headers=initiated.required_headers,
        )
        response.raise_for_status()
    async with factory() as session:
        completed = await upload_service(session, store).complete(
            project_id=project.id,
            upload_id=initiated.upload_id,
            principal=principal,
        )
        await session.commit()
        asset = await session.get(Asset, completed.asset_id)
        project = await session.get(Project, project.id)
        if asset is None or project is None:
            raise RuntimeError("Completed ORBIT source upload is not resolvable.")
        _emit("seed.source_stored", sha256=asset.sha256, size_bytes=asset.size_bytes)
        return principal, project, asset


def _brief_hash() -> str:
    normalized = " ".join(unicodedata.normalize("NFC", DEFAULT_BRIEF_TEXT).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


async def _ensure_build(project: Project, product: Asset) -> Build:
    factory = get_session_factory()
    async with factory() as session:
        current_project = await session.get(Project, project.id)
        if current_project is None or current_project.active_revision_id is None:
            raise RuntimeError("ORBIT project has no active source revision.")
        existing = await session.scalar(
            select(Build)
            .where(
                Build.project_id == project.id,
                Build.project_revision_id == current_project.active_revision_id,
            )
            .order_by(Build.created_at.desc())
            .limit(1)
        )
        if existing is not None and existing.status != str(BuildStatus.FAILED):
            return existing
        graph = await session.scalar(
            select(GraphRevision).where(
                GraphRevision.project_revision_id == current_project.active_revision_id
            )
        )
        if graph is None:
            raise RuntimeError("ORBIT active revision has no graph.")
        graph_nodes = (
            await session.scalars(select(GraphNode).where(GraphNode.graph_revision_id == graph.id))
        ).all()
        if len(graph_nodes) != 18:
            raise RuntimeError("ORBIT graph is not the required 18-node template.")
        parent = existing if existing is not None else None
        previous_nodes = {}
        if parent is not None:
            previous_nodes = {
                node.stable_key: node
                for node in await session.scalars(
                    select(BuildNode).where(BuildNode.build_id == parent.id)
                )
            }
        reusable_keys = {
            stable_key
            for stable_key, node in previous_nodes.items()
            if BuildNodeStatus(node.status).satisfies_dependency
            and node.selected_asset_set_hash is not None
        }
        build = Build(
            id=uuid.uuid4(),
            project_id=project.id,
            project_revision_id=current_project.active_revision_id,
            graph_revision_id=graph.id,
            status=str(BuildStatus.QUEUED),
            total_nodes=18,
            parent_build_id=parent.id if parent is not None else None,
            reused_nodes=len(reusable_keys),
            rebuilt_nodes=18 - len(reusable_keys),
            is_fixture=False,
            version=1,
        )
        session.add(build)
        source_hashes = {
            "source.brief": _brief_hash(),
            "source.product_reference": product.sha256,
        }
        for graph_node in graph_nodes:
            compiled = CompiledNode.model_validate(graph_node.spec_json)
            is_source = graph_node.stable_key in source_hashes
            selected_hash = source_hashes.get(graph_node.stable_key)
            previous = previous_nodes.get(graph_node.stable_key)
            reused = graph_node.stable_key in reusable_keys and previous is not None
            fingerprint = (
                compute_source_fingerprint(compiled, content_hash=selected_hash)
                if selected_hash is not None
                else graph_node.spec_hash
            )
            proof = None
            if graph_node.stable_key == "source.product_reference":
                proof = {"asset_ids": [str(product.id)], "validations_current": True}
            if reused and previous is not None:
                fingerprint = previous.fingerprint
                selected_hash = previous.selected_asset_set_hash
                proof = previous.reuse_proof_json
            node = BuildNode(
                id=uuid.uuid4(),
                build_id=build.id,
                graph_node_id=graph_node.id,
                stable_key=graph_node.stable_key,
                fingerprint=fingerprint,
                status=str(
                    BuildNodeStatus.REUSED
                    if reused
                    else BuildNodeStatus.PASSED
                    if is_source
                    else BuildNodeStatus.PENDING
                ),
                resolution="REUSED" if reused else "SOURCE" if is_source else "REBUILT",
                reason_code="EXACT_VALIDATED_REUSE" if reused else None,
                reason=(
                    f"Reused from failed parent build {parent.id}."
                    if reused and parent is not None
                    else None
                ),
                selected_attempt_id=(
                    previous.selected_attempt_id if reused and previous is not None else None
                ),
                selected_asset_set_hash=selected_hash,
                reuse_proof_json=proof,
                version=1,
            )
            session.add(node)
        await session.flush()
        for stable_key in source_hashes:
            if stable_key in reusable_keys:
                continue
            session.add(
                DomainEvent(
                    event_id=uuid.uuid4(),
                    organization_id=current_project.organization_id,
                    project_id=current_project.id,
                    build_id=build.id,
                    event_type="build.node.status_changed",
                    payload_json={
                        "stable_key": stable_key,
                        "from": "PENDING",
                        "to": "PASSED",
                        "source": "VERIFIED_SOURCE",
                    },
                    correlation_id=uuid.uuid4(),
                )
            )
        session.add(
            DomainEvent(
                event_id=uuid.uuid4(),
                organization_id=current_project.organization_id,
                project_id=current_project.id,
                build_id=build.id,
                event_type="build.created",
                payload_json={
                    "status": build.status,
                    "total_nodes": 18,
                    "reused_nodes": len(reusable_keys),
                    "rebuilt_nodes": 18 - len(reusable_keys),
                    "is_fixture": False,
                    "parent_build_id": str(parent.id) if parent is not None else None,
                },
                correlation_id=uuid.uuid4(),
            )
        )
        await schedule_ready_nodes(session, build, current_project)
        await session.commit()
        _emit(
            "seed.build_created",
            build_id=str(build.id),
            parent_build_id=str(parent.id) if parent is not None else None,
            reused_nodes=len(reusable_keys),
            rebuilt_nodes=18 - len(reusable_keys),
            queued=True,
        )
        return build


def _runtime(store: B2Store) -> WorkerRuntime:
    environment = dict(os.environ)
    factory = get_session_factory()
    gateway = GMICloudGateway(
        GMICloudSettings.from_env(environment),
        B2Settings.from_env(environment),
    )
    return WorkerRuntime(
        factory,
        store,
        owner="orbit-seed-worker",
        lease_seconds=int(environment["WORK_LEASE_SECONDS"]),
        heartbeat_seconds=int(environment["WORK_HEARTBEAT_SECONDS"]),
        concurrency=min(2, int(environment["WORKER_CONCURRENCY"])),
        gmi_handlers=GMIWorkHandlers(
            factory,
            store,
            gateway,
            environment=environment,
        ),
    )


async def _run_build(build_id: uuid.UUID, store: B2Store) -> Build:
    runtime = _runtime(store)
    factory = get_session_factory()
    idle_rounds = 0
    while True:
        receipt = await runtime.run_once()
        async with factory() as session:
            build = await session.get(Build, build_id)
            if build is None:
                raise RuntimeError("ORBIT build disappeared.")
            count_rows = (
                await session.execute(
                    select(BuildNode.status, func.count(BuildNode.id))
                    .where(BuildNode.build_id == build.id)
                    .group_by(BuildNode.status)
                )
            ).all()
            counts: dict[str, int] = {status: count for status, count in count_rows}
            _emit(
                "seed.worker_batch",
                claimed=receipt.claimed,
                completed=receipt.completed,
                failed=receipt.failed,
                build_status=build.status,
                node_counts=counts,
            )
            if BuildStatus(build.status).is_terminal:
                if build.status != str(BuildStatus.SUCCEEDED):
                    failures = (
                        await session.execute(
                            select(BuildNode.stable_key, BuildNode.status, BuildNode.reason).where(
                                BuildNode.build_id == build.id,
                                BuildNode.status.in_({"FAILED", "WAITING_REVIEW"}),
                            )
                        )
                    ).all()
                    raise RuntimeError(f"ORBIT build ended {build.status}: {failures}")
                return build
        if receipt.claimed == 0:
            idle_rounds += 1
            if idle_rounds >= 3:
                raise RuntimeError("ORBIT build is non-terminal but has no claimable work.")
            await asyncio.sleep(1)
        else:
            idle_rounds = 0


async def _verify_work_assets(build: Build, store: B2Store) -> int:
    factory = get_session_factory()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Asset)
                    .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
                    .join(BuildNode, BuildNode.selected_attempt_id == AttemptAsset.attempt_id)
                    .where(
                        BuildNode.build_id == build.id,
                        AttemptAsset.selected.is_(True),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        product = await session.scalar(
            select(Asset).where(
                Asset.organization_id
                == (
                    select(Project.organization_id)
                    .where(Project.id == build.project_id)
                    .scalar_subquery()
                ),
                Asset.sha256
                == (
                    select(BuildNode.selected_asset_set_hash)
                    .where(
                        BuildNode.build_id == build.id,
                        BuildNode.stable_key == "source.product_reference",
                    )
                    .scalar_subquery()
                ),
            )
        )
        if product is not None:
            rows.append(product)
    for asset in rows:
        if not await asyncio.to_thread(store.verify, asset.b2_key, expected_sha256=asset.sha256):
            raise RuntimeError(f"Stored-byte verification failed for asset {asset.id}")
    _emit("seed.work_assets_verified", checked_assets=len(rows))
    return len(rows)


async def _persist_demo_proof(principal: Principal, project: Project, build: Build) -> None:
    factory = get_session_factory()
    async with factory() as session:
        service = ChangeImpactService(session, environment=dict(os.environ))
        change_set = await service.create(
            project_id=project.id,
            principal=principal,
            request=ChangeSetCreateRequest(
                base_revision_id=build.project_revision_id,
                patch=RevisionPatch(
                    parameters=RevisionParametersPatch(legal_line="no added sugar")
                ),
            ),
        )
        plan = await service.impact(change_set_id=change_set.id, principal=principal)
        revised_graph = compile_graph(
            ORBIT_TEMPLATE,
            parameters={
                PARAM_LEGAL_LINE: "no added sugar",
                PARAM_BRIEF_TEXT: DEFAULT_BRIEF_TEXT,
            },
            policy_hashes=orbit_policy_hashes(),
        )
        labels = {node.stable_key: node.label for node in revised_graph.nodes}
        rebuilt = [
            {
                "stable_key": node.stable_key,
                "label": labels[node.stable_key],
                "decision": str(node.decision),
                "reason_code": str(node.reason_code),
                "reason": node.reason,
                "provider_calls": node.provider_calls,
            }
            for node in plan.nodes
            if str(node.decision) == "REBUILD"
        ]
        current_project = await session.get(Project, project.id)
        if current_project is None:
            raise RuntimeError("ORBIT project disappeared before proof event.")
        payload = {
            "schema_version": "1",
            "template": revised_graph.template_version_label,
            "total_nodes": len(plan.nodes),
            "reuse": plan.summary.reuse,
            "rebuild": plan.summary.rebuild,
            "review": plan.summary.review,
            "blocked": plan.summary.blocked,
            "provider_calls": plan.summary.provider_calls,
            "pricing_status": str(plan.summary.pricing_status),
            "estimated_cost_usd": plan.summary.estimated_cost_usd,
            "change_from": DEFAULT_LEGAL_LINE,
            "change_to": "no added sugar",
            "rebuild_nodes": rebuilt,
            "plan_hash": plan.plan_hash,
            "graph_hash": revised_graph.canonical_hash,
        }
        latest = await session.scalar(
            select(DomainEvent)
            .where(
                DomainEvent.build_id == build.id,
                DomainEvent.event_type == "demo.proof.computed",
            )
            .order_by(DomainEvent.sequence.desc())
            .limit(1)
        )
        if latest is not None and latest.payload_json == payload:
            await session.rollback()
            _emit(
                "seed.proof_event_current",
                reuse=plan.summary.reuse,
                rebuild=plan.summary.rebuild,
                source="BUILD_EVENTS",
            )
            return
        session.add(
            DomainEvent(
                event_id=uuid.uuid4(),
                organization_id=current_project.organization_id,
                project_id=current_project.id,
                build_id=build.id,
                event_type="demo.proof.computed",
                payload_json=payload,
                correlation_id=uuid.uuid4(),
            )
        )
        await session.commit()
        _emit(
            "seed.proof_event_stored",
            reuse=plan.summary.reuse,
            rebuild=plan.summary.rebuild,
            source="BUILD_EVENTS",
            superseded=latest is not None,
        )


async def _publish_v1(
    principal: Principal,
    build: Build,
    work_store: B2Store,
    release_store: B2Store,
) -> Release:
    factory = get_session_factory()
    async with factory() as session:
        service = ReleaseService(session, work_store, release_store)
        candidate = await service.create_candidate(
            build_id=build.id,
            version_label="v1",
            principal=principal,
        )
        if candidate.status == "READY_FOR_APPROVAL":
            candidate = await service.approve(
                release_id=candidate.id,
                principal=principal,
                reason="Initial ORBIT baseline verified for public demo.",
            )
        if candidate.status == "APPROVED":
            candidate = await service.publish(
                release_id=candidate.id,
                principal=principal,
                reason="Publish initial immutable ORBIT release v1.",
            )
        proof = await service.verify(release_id=candidate.id)
        await session.commit()
        release = await session.get(Release, candidate.id)
        if release is None:
            raise RuntimeError("Published release disappeared.")
        _emit(
            "seed.release_published",
            release_id=str(release.id),
            version_label=release.version_label,
            status=release.status,
            checked_assets=proof.checked_assets,
            manifest_sha256=proof.manifest_sha256,
            retention_mode=proof.retention_mode,
        )
        return release


async def run(product_path: Path) -> None:
    _load_env()
    environment = dict(os.environ)
    work_store = B2Store(B2Settings.from_env(environment), preflight=True)
    release_store = B2Store(B2Settings.from_env(environment, release=True), preflight=True)
    try:
        principal, project, product = await _ensure_project(product_path, work_store)
        build = await _ensure_build(project, product)
        build = await _run_build(build.id, work_store)
        await _verify_work_assets(build, work_store)
        await _persist_demo_proof(principal, project, build)
        await _publish_v1(principal, build, work_store, release_store)
    finally:
        work_store.close()
        release_store.close()
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--product-reference",
        type=Path,
        default=Path("assets/orbit/product-reference.png"),
    )
    args = parser.parse_args()
    if not args.product_reference.is_file():
        raise SystemExit(f"Product reference not found: {args.product_reference}")
    try:
        asyncio.run(run(args.product_reference.resolve()))
    except Exception as exc:
        _emit("seed.failed", error_type=type(exc).__name__, message=str(exc)[:500])
        raise


if __name__ == "__main__":
    main()
