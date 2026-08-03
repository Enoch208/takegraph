"""Resolve source nodes that a revision change invalidated (PRD §4.2, §12.2).

A source node generates nothing. Its output is the content the project revision
already holds — normalized brief text, or an uploaded and verified asset — so
"executing" one means resolving that content and recording the hash.

This handler exists because editing the brief is a legitimate user action that
previously produced an unrunnable build: the impact engine correctly marked
`source.brief` REBUILD, the node was queued, and the worker had no handler for
it, so the build stalled on its first node with `unsupported build node`.

No provider is contacted and no bytes are written. §12.2 defines a source
fingerprint as "the verified source content hash plus normalization/version
metadata", and that hash is already knowable from the revision.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from takegraph_api.db.models import (
    Asset,
    Build,
    BuildNode,
    DomainEvent,
    GraphNode,
    Project,
    ProjectRevision,
    Source,
    SourceVersion,
)
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.canonical import JsonValue
from takegraph_domain.enums import BuildNodeStatus, BuildStatus
from takegraph_domain.errors import AssetVerificationError, InvalidSourceError, NotFoundError
from takegraph_domain.graph.source_content import brief_hash_from_spec

from takegraph_worker.build_work import schedule_ready_nodes

SOURCE_KEYS = frozenset({"source.brief", "source.product_reference"})


def brief_content_hash(spec: dict[str, JsonValue]) -> str:
    """Content hash for the normalized brief.

    Must match the hash the impact engine uses when it decides whether this node
    can be reused, so both call one shared definition. They did not, once, and a
    resolved source node could never satisfy the reuse proof — see
    takegraph_domain.graph.source_content for what that cost.
    """
    return brief_hash_from_spec(dict(spec))


class SourceNodeHandlers:
    """Resolve SOURCE_TEXT and SOURCE_IMAGE build nodes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute_build_node(self, build_node_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            node = await session.scalar(
                select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
            )
            if node is None:
                raise NotFoundError("Source build node was not found.")
            build = await session.scalar(
                select(Build).where(Build.id == node.build_id).with_for_update()
            )
            if build is None:
                raise NotFoundError("Source build was not found.")
            project = await session.get(Project, build.project_id)
            graph_node = await session.get(GraphNode, node.graph_node_id)
            if project is None or graph_node is None:
                raise InvalidSourceError("Source node references incomplete graph data.")
            if node.status != str(BuildNodeStatus.QUEUED):
                raise InvalidSourceError(f"Source node is not runnable from {node.status}.")
            if build.status not in {str(BuildStatus.QUEUED), str(BuildStatus.RUNNING)}:
                raise InvalidSourceError(f"Source build is not runnable from {build.status}.")

            revision = await session.get(ProjectRevision, build.project_revision_id)
            if revision is None:
                raise InvalidSourceError("Source node has no project revision to resolve against.")

            if graph_node.node_type == "SOURCE_TEXT":
                content_hash = brief_content_hash(revision.spec_json)
            elif graph_node.node_type == "SOURCE_IMAGE":
                content_hash = await self._verified_image_hash(session, project, node)
            else:
                raise InvalidSourceError(
                    f"Source handler cannot resolve node type {graph_node.node_type}."
                )

            # The full chain, not a shortcut to PASSED. §5.4 FR-BUILD-007 requires
            # storage and verification before a node can satisfy a dependency, and
            # that holds for a source too: STORING is a no-op because the content
            # is already durable, but VERIFYING is real — the SOURCE_IMAGE path
            # above refuses bytes B2 has not confirmed.
            for target in (
                BuildNodeStatus.RUNNING,
                BuildNodeStatus.STORING,
                BuildNodeStatus.VERIFYING,
                BuildNodeStatus.PASSED,
            ):
                assert_transition(BuildNodeStatus(node.status), target, subject="node")
                node.status = str(target)
                if target is BuildNodeStatus.RUNNING:
                    node.started_at = datetime.now(UTC)

            # No attempt row: nothing was submitted anywhere, and recording one
            # would put a provider call into the evidence that never happened.
            node.resolution = "SOURCE"
            node.selected_asset_set_hash = content_hash
            node.completed_at = datetime.now(UTC)
            node.version += 1

            if build.status == str(BuildStatus.QUEUED):
                assert_transition(BuildStatus.QUEUED, BuildStatus.RUNNING, subject="build")
                build.status = str(BuildStatus.RUNNING)
                build.started_at = datetime.now(UTC)

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
                        "from": str(BuildNodeStatus.QUEUED),
                        "to": str(BuildNodeStatus.PASSED),
                        "resolution": "SOURCE",
                    },
                    correlation_id=uuid.uuid4(),
                )
            )
            await session.flush()
            await schedule_ready_nodes(session, build, project)
            await session.commit()

    async def _verified_image_hash(
        self, session: AsyncSession, project: Project, node: BuildNode
    ) -> str:
        """The uploaded source's hash, only if B2 verified its bytes.

        §8.3.7 forbids trusting a declared hash. An unverified source must not
        satisfy a dependency, so this raises rather than passing the node.
        """
        row = await session.execute(
            select(SourceVersion, Asset)
            .join(Source, Source.id == SourceVersion.source_id)
            .outerjoin(Asset, Asset.id == SourceVersion.asset_id)
            .where(Source.project_id == project.id, Source.stable_key == node.stable_key)
            .order_by(SourceVersion.created_at.desc())
            .limit(1)
        )
        found = row.one_or_none()
        source_version: SourceVersion | None = None if found is None else found[0]
        asset: Asset | None = None if found is None else found[1]
        if source_version is None or asset is None:
            raise InvalidSourceError(
                f"{node.stable_key} has no uploaded source version to resolve."
            )
        if asset.verified_at is None or source_version.content_hash != asset.sha256:
            raise AssetVerificationError(
                f"{node.stable_key} references bytes that B2 has not verified."
            )
        return source_version.content_hash
