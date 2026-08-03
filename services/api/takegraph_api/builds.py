"""Build and node read surface (PRD §11.6).

This is what the storyboard and node inspector render from. §18.10 wants each
node to expose its purpose, inputs, attempts, validations, assets and lineage, so
the graph response carries all of it in one round trip rather than making the
client fan out per node — eighteen nodes times four sub-resources is a lot of
requests for a page that must feel instant to a judge.

Assets are returned as an `access_path`, never a signed URL. §15.3 is explicit
that signed URLs are not persisted and are generated on demand after
authorization, so the client asks for one when it actually needs to render.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.errors import NotFoundError

from takegraph_api.auth import get_principal
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
from takegraph_api.db.session import session_scope

router = APIRouter(prefix="/api/v1", tags=["builds"])

#: Poll cadence for the event stream. §20.1 targets p95 SSE latency under two
#: seconds from a committed event, so one second leaves headroom without turning
#: the stream into a busy loop.
_SSE_POLL_SECONDS = 1.0

#: Stop after this many consecutive quiet ticks (~5 min). A finished build emits
#: nothing further, and holding the connection open forever leaks a worker slot
#: per abandoned browser tab. The client reconnects with Last-Event-ID.
_SSE_MAX_IDLE_TICKS = 300


class AssetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    role: str
    ordinal: int
    selected: bool
    sha256: str
    size_bytes: int
    mime_type: str
    media_kind: str
    derived_from_asset_id: uuid.UUID | None
    verified_at: str | None
    access_path: str
    """Where to request a short-lived signed URL. Not the URL itself (§15.3)."""


class AttemptEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    event_type: str
    received_at: str
    provider_timestamp: str | None


class AttemptView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    attempt_no: int
    parent_attempt_id: uuid.UUID | None
    mechanism: str
    provider: str | None
    model: str | None
    status: str
    error_class: str | None
    error_code: str | None
    error_message: str | None
    is_injected_fault: bool
    """§4.4: the UI labels an injected failure TEST FAULT from this flag, so the
    label comes from stored evidence rather than a UI guess."""
    estimated_cost_usd: str | None
    provider_reported_cost_usd: str | None
    latency_ms: int | None
    submitted_at: str | None
    completed_at: str | None
    created_at: str
    events: list[AttemptEventView]
    assets: list[AssetView]


class ValidationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    attempt_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    policy_id: uuid.UUID | None
    gate_key: str
    gate_version: str
    status: str
    score: str | None
    confidence: str | None
    evidence: dict | None
    created_at: str


class BuildNodeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    stable_key: str
    label: str
    node_type: str
    status: str
    current_activity: str | None
    resolution: str | None
    reason_code: str | None
    reason: str | None
    fingerprint: str
    selected_asset_set_hash: str | None
    source_build_node_id: uuid.UUID | None
    started_at: str | None
    completed_at: str | None
    created_at: str
    selected_attempt: AttemptView | None
    attempts: list[AttemptView]
    selected_assets: list[AssetView]
    validations: list[ValidationView]


class BuildSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    project_revision_id: uuid.UUID
    graph_revision_id: uuid.UUID
    impact_plan_id: uuid.UUID | None
    parent_build_id: uuid.UUID | None
    status: str
    total_nodes: int
    reused_nodes: int
    rebuilt_nodes: int
    estimated_cost_usd: str | None
    provider_reported_cost_usd: str | None
    is_fixture: bool
    started_at: str | None
    completed_at: str | None
    created_at: str
    latest_event_sequence: int


class BuildGraphView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build: BuildSummaryView
    latest_event_sequence: int
    nodes: list[BuildNodeView]


def _iso(value: object) -> str | None:
    return None if value is None else value.isoformat()  # type: ignore[union-attr]


def _decimal(value: object) -> str | None:
    """Money crosses the wire as a string. §8.1 stores it as numeric(14,6), and
    serialising through a float would reintroduce the rounding that column exists
    to avoid."""
    return None if value is None else str(value)


#: Normalised activity labels (§18.9). The raw status is an implementation
#: detail; a judge reads "Storing in B2", not "STORING".
_ACTIVITY = {
    "PENDING": None,
    "QUEUED": "Preparing",
    "RUNNING": "Generating",
    "STORING": "Storing in B2",
    "VERIFYING": "Verifying",
    "PASSED": "Passed",
    "REUSED": "Reused",
    "WAITING_REVIEW": "Waiting for review",
    "RETRY_PENDING": "Retrying",
    "FALLBACK_PENDING": "Falling back to another provider",
    "RETAKE_PENDING": "Retaking",
    "FAILED": "Failed",
    "CANCELLED": "Cancelled",
}


def _asset_views(rows: list[tuple[AttemptAsset, Asset]]) -> list[AssetView]:
    return [
        AssetView(
            id=asset.id,
            role=link.role,
            ordinal=link.ordinal,
            selected=link.selected,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            mime_type=asset.mime_type,
            media_kind=asset.media_kind,
            derived_from_asset_id=asset.derived_from_asset_id,
            verified_at=_iso(asset.verified_at),
            access_path=f"/api/v1/assets/{asset.id}/access",
        )
        for link, asset in rows
    ]


def _reused_asset_views(node: BuildNode, assets: dict[uuid.UUID, Asset]) -> list[AssetView]:
    """Assets a REUSED node inherited, resolved from its persisted reuse proof.

    Marked `selected=True` because that is what they are for this build — the
    node's accepted output — even though the AttemptAsset row that originally
    carried the flag belongs to the build that generated them.
    """
    proof = node.reuse_proof_json or {}
    views: list[AssetView] = []
    for ordinal, raw in enumerate(proof.get("asset_ids", [])):
        try:
            asset = assets.get(uuid.UUID(str(raw)))
        except ValueError:
            continue
        if asset is None:
            continue
        views.append(
            AssetView(
                id=asset.id,
                role="primary",
                ordinal=ordinal,
                selected=True,
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                mime_type=asset.mime_type,
                media_kind=asset.media_kind,
                derived_from_asset_id=asset.derived_from_asset_id,
                verified_at=_iso(asset.verified_at),
                access_path=f"/api/v1/assets/{asset.id}/access",
            )
        )
    return views


async def _load_build(build_id: uuid.UUID, principal: Principal) -> tuple[Build, Project]:
    async with session_scope() as session:
        build = await session.get(Build, build_id)
        if build is None:
            raise NotFoundError("Build not found.")
        project = await session.get(Project, build.project_id)
        if project is None:
            raise NotFoundError("Build not found.")
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.VIEW_PROJECT,
        )
        return build, project


async def _latest_sequence(session, build_id: uuid.UUID) -> int:
    """Used by the client to resume SSE with Last-Event-ID (§5.9 FR-EVT-001)."""
    value = await session.scalar(
        select(func.max(DomainEvent.sequence)).where(DomainEvent.build_id == build_id)
    )
    return int(value or 0)


def _summary(build: Build, latest_sequence: int) -> BuildSummaryView:
    return BuildSummaryView(
        id=build.id,
        project_id=build.project_id,
        project_revision_id=build.project_revision_id,
        graph_revision_id=build.graph_revision_id,
        impact_plan_id=build.impact_plan_id,
        parent_build_id=build.parent_build_id,
        status=build.status,
        total_nodes=build.total_nodes,
        reused_nodes=build.reused_nodes,
        rebuilt_nodes=build.rebuilt_nodes,
        estimated_cost_usd=_decimal(build.estimated_cost_usd),
        provider_reported_cost_usd=_decimal(build.provider_reported_cost_usd),
        is_fixture=build.is_fixture,
        started_at=_iso(build.started_at),
        completed_at=_iso(build.completed_at),
        created_at=_iso(build.created_at) or "",
        latest_event_sequence=latest_sequence,
    )


@router.get("/builds/{build_id}/events")
async def stream_build_events(
    build_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Ordered build events as SSE (§11.6, §5.9 FR-EVT-001).

    `id:` is the authoritative `domain_events.sequence` (§11.1), so a client that
    reconnects with `Last-Event-ID` resumes exactly where it stopped rather than
    replaying or skipping. Events are read from PostgreSQL, not Redis — §6.3 makes
    the database authoritative, so a Redis outage degrades latency rather than
    losing events.
    """
    await _load_build(build_id, principal)

    try:
        cursor = int(last_event_id) if last_event_id else 0
    except ValueError:
        # A malformed resume header must not silently replay the whole stream.
        raise NotFoundError("Last-Event-ID must be a domain event sequence.") from None

    async def emit() -> AsyncIterator[bytes]:
        position = cursor
        idle = 0
        while idle < _SSE_MAX_IDLE_TICKS:
            async with session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(DomainEvent)
                            .where(
                                DomainEvent.build_id == build_id,
                                DomainEvent.sequence > position,
                            )
                            .order_by(DomainEvent.sequence)
                            .limit(200)
                        )
                    )
                    .scalars()
                    .all()
                )

            if rows:
                idle = 0
                for event in rows:
                    position = event.sequence
                    payload = {
                        "schema_version": "1",
                        "sequence": event.sequence,
                        "event_id": str(event.event_id),
                        "event_type": event.event_type,
                        "build_id": str(event.build_id) if event.build_id else None,
                        "project_id": str(event.project_id) if event.project_id else None,
                        "occurred_at": _iso(event.occurred_at),
                        "payload": event.payload_json,
                    }
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(payload)}\n\n"
                    ).encode()
            else:
                idle += 1
                # A comment frame keeps proxies from closing an idle connection
                # without the client mistaking it for an event.
                yield b": keep-alive\n\n"

            await asyncio.sleep(_SSE_POLL_SECONDS)

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/builds/{build_id}", response_model=BuildSummaryView)
async def get_build(
    build_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> BuildSummaryView:
    build, _ = await _load_build(build_id, principal)
    async with session_scope() as session:
        return _summary(build, await _latest_sequence(session, build_id))


@router.get("/builds/{build_id}/graph", response_model=BuildGraphView)
async def get_build_graph(
    build_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> BuildGraphView:
    """Nodes, edges, current statuses and reasons (§11.6).

    Loaded in a handful of queries keyed by build rather than per node. The
    storyboard needs everything at once, and eighteen nodes each fanning out to
    attempts, events, validations and assets would otherwise be ~70 round trips.
    """
    build, _ = await _load_build(build_id, principal)

    async with session_scope() as session:
        node_rows = (
            await session.execute(
                select(BuildNode, GraphNode)
                .join(GraphNode, GraphNode.id == BuildNode.graph_node_id)
                .where(BuildNode.build_id == build_id)
                .order_by(BuildNode.created_at, BuildNode.stable_key)
            )
        ).all()
        node_ids = [node.id for node, _ in node_rows]

        attempts = (
            (
                await session.execute(
                    select(Attempt)
                    .where(Attempt.build_node_id.in_(node_ids))
                    .order_by(Attempt.build_node_id, Attempt.attempt_no)
                )
            )
            .scalars()
            .all()
        )
        attempt_ids = [a.id for a in attempts]

        event_rows = (
            (
                await session.execute(
                    select(AttemptEvent)
                    .where(AttemptEvent.attempt_id.in_(attempt_ids))
                    .order_by(AttemptEvent.attempt_id, AttemptEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        asset_rows = (
            await session.execute(
                select(AttemptAsset, Asset)
                .join(Asset, Asset.id == AttemptAsset.asset_id)
                .where(AttemptAsset.attempt_id.in_(attempt_ids))
                .order_by(AttemptAsset.attempt_id, AttemptAsset.ordinal)
            )
        ).all()
        validation_rows = (
            (
                await session.execute(
                    select(Validation)
                    .where(Validation.build_node_id.in_(node_ids))
                    .order_by(Validation.build_node_id, Validation.created_at)
                )
            )
            .scalars()
            .all()
        )

        events_by_attempt: dict[uuid.UUID, list[AttemptEvent]] = {}
        for event in event_rows:
            events_by_attempt.setdefault(event.attempt_id, []).append(event)

        assets_by_attempt: dict[uuid.UUID, list[tuple[AttemptAsset, Asset]]] = {}
        for link, asset in asset_rows:
            assets_by_attempt.setdefault(link.attempt_id, []).append((link, asset))

        validations_by_node: dict[uuid.UUID, list[Validation]] = {}
        for validation in validation_rows:
            validations_by_node.setdefault(validation.build_node_id, []).append(validation)

        # A REUSED node has no attempt in *this* build — its bytes were produced
        # by an earlier one — so its media has to come from the reuse proof
        # (§12.3 persists the exact asset ids). Without this, an incremental
        # build renders thirteen empty cards and the reuse story it exists to
        # tell is invisible.
        reused_asset_ids: set[uuid.UUID] = set()
        for node, _ in node_rows:
            proof = node.reuse_proof_json or {}
            for raw in proof.get("asset_ids", []):
                try:
                    reused_asset_ids.add(uuid.UUID(str(raw)))
                except ValueError:
                    continue

        reused_assets: dict[uuid.UUID, Asset] = {}
        if reused_asset_ids:
            reused_assets = {
                asset.id: asset
                for asset in (
                    (await session.execute(select(Asset).where(Asset.id.in_(reused_asset_ids))))
                    .scalars()
                    .all()
                )
            }

        attempts_by_node: dict[uuid.UUID, list[AttemptView]] = {}
        attempt_views: dict[uuid.UUID, AttemptView] = {}
        for attempt in attempts:
            view = AttemptView(
                id=attempt.id,
                attempt_no=attempt.attempt_no,
                parent_attempt_id=attempt.parent_attempt_id,
                mechanism=attempt.mechanism,
                provider=attempt.provider,
                model=attempt.model,
                status=attempt.status,
                error_class=attempt.error_class,
                error_code=attempt.error_code,
                error_message=attempt.error_message,
                is_injected_fault=attempt.is_injected_fault,
                estimated_cost_usd=_decimal(attempt.estimated_cost_usd),
                provider_reported_cost_usd=_decimal(attempt.provider_reported_cost_usd),
                latency_ms=attempt.latency_ms,
                submitted_at=_iso(attempt.submitted_at),
                completed_at=_iso(attempt.completed_at),
                created_at=_iso(attempt.created_at) or "",
                events=[
                    AttemptEventView(
                        sequence=e.sequence,
                        event_type=e.provider_event_type,
                        received_at=_iso(e.received_at) or "",
                        provider_timestamp=_iso(e.provider_timestamp),
                    )
                    for e in events_by_attempt.get(attempt.id, [])
                ],
                assets=_asset_views(assets_by_attempt.get(attempt.id, [])),
            )
            attempt_views[attempt.id] = view
            attempts_by_node.setdefault(attempt.build_node_id, []).append(view)

        nodes: list[BuildNodeView] = []
        for node, graph_node in node_rows:
            selected = (
                attempt_views.get(node.selected_attempt_id) if node.selected_attempt_id else None
            )
            nodes.append(
                BuildNodeView(
                    id=node.id,
                    stable_key=node.stable_key,
                    label=graph_node.label,
                    node_type=graph_node.node_type,
                    status=node.status,
                    current_activity=_ACTIVITY.get(node.status),
                    resolution=node.resolution,
                    reason_code=node.reason_code,
                    reason=node.reason,
                    fingerprint=node.fingerprint,
                    selected_asset_set_hash=node.selected_asset_set_hash,
                    source_build_node_id=(
                        uuid.UUID(node.reuse_proof_json["source_build_node_id"])
                        if node.reuse_proof_json
                        and node.reuse_proof_json.get("source_build_node_id")
                        else None
                    ),
                    started_at=_iso(node.started_at),
                    completed_at=_iso(node.completed_at),
                    created_at=_iso(node.created_at) or "",
                    selected_attempt=selected,
                    attempts=attempts_by_node.get(node.id, []),
                    selected_assets=(
                        [a for a in selected.assets if a.selected]
                        if selected
                        else _reused_asset_views(node, reused_assets)
                    ),
                    validations=[
                        ValidationView(
                            id=v.id,
                            attempt_id=v.attempt_id,
                            asset_id=v.asset_id,
                            policy_id=v.policy_id,
                            gate_key=v.gate_key,
                            gate_version=v.gate_version,
                            status=v.status,
                            score=_decimal(v.score),
                            confidence=_decimal(v.confidence),
                            evidence=v.evidence_json,
                            created_at=_iso(v.created_at) or "",
                        )
                        for v in validations_by_node.get(node.id, [])
                    ],
                )
            )

        latest = await _latest_sequence(session, build_id)
        return BuildGraphView(
            build=_summary(build, latest),
            latest_event_sequence=latest,
            nodes=nodes,
        )
