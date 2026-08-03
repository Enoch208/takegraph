"""Release candidate, approval, publication, and verification services."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.canonical import JsonValue, canonical_bytes
from takegraph_domain.enums import BuildNodeStatus, BuildStatus, ReleaseStatus
from takegraph_domain.errors import (
    AssetVerificationError,
    InvalidSourceError,
    NotFoundError,
    ReleaseNotReadyError,
)
from takegraph_domain.storage.keys import release_key
from takegraph_infrastructure.b2 import B2Store

from takegraph_api.db.models import (
    Approval,
    Asset,
    AttemptAsset,
    AuditLog,
    Build,
    BuildNode,
    DomainEvent,
    GraphNode,
    Project,
    Release,
    ReleaseAsset,
)

DELIVERABLE_KEYS = frozenset({"compose.delivery_package", "image.poster"})


class ReleaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    build_id: uuid.UUID
    version_label: str
    status: str
    manifest_asset_id: uuid.UUID | None
    verification_asset_id: uuid.UUID | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    published_at: datetime | None
    retention_mode: str | None
    asset_count: int


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


class VerificationResponse(BaseModel):
    release_id: uuid.UUID
    verified: bool
    checked_assets: int
    manifest_sha256: str
    retention_mode: str
    verified_at: datetime
    """When this check ran. A verification is a statement about a moment — the
    bytes were re-read and re-hashed then — so the answer is worth little without
    it, and the proof page renders it as evidence."""


class ReleaseService:
    def __init__(
        self,
        session: AsyncSession,
        work_store: B2Store,
        release_store: B2Store,
    ) -> None:
        self._session = session
        self._work_store = work_store
        self._release_store = release_store

    async def create_candidate(
        self,
        *,
        build_id: uuid.UUID,
        version_label: str,
        principal: Principal,
    ) -> ReleaseResponse:
        build = await self._session.get(Build, build_id)
        if build is None:
            raise NotFoundError("Build was not found.")
        project = await self._session.get(Project, build.project_id)
        if project is None:
            raise NotFoundError("Release project was not found.")
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.PUBLISH_RELEASE,
        )
        if build.status != str(BuildStatus.SUCCEEDED):
            raise ReleaseNotReadyError("Only a successful build can become a release.")
        existing = await self._session.scalar(
            select(Release).where(
                Release.project_id == project.id,
                Release.version_label == version_label,
            )
        )
        if existing is not None:
            return await self._response(existing)
        node_rows = (
            await self._session.execute(
                select(BuildNode, GraphNode)
                .join(GraphNode, GraphNode.id == BuildNode.graph_node_id)
                .where(BuildNode.build_id == build.id)
            )
        ).all()
        if len(node_rows) != build.total_nodes or any(
            graph_node.required
            and BuildNodeStatus(node.status)
            not in {
                BuildNodeStatus.PASSED,
                BuildNodeStatus.REUSED,
            }
            for node, graph_node in node_rows
        ):
            raise ReleaseNotReadyError("Required build nodes do not all have accepted output.")
        deliverable_node_ids = [
            node.id for node, _ in node_rows if node.stable_key in DELIVERABLE_KEYS
        ]
        asset_rows = (
            await self._session.execute(
                select(BuildNode.stable_key, AttemptAsset, Asset)
                .join(AttemptAsset, AttemptAsset.attempt_id == BuildNode.selected_attempt_id)
                .join(Asset, Asset.id == AttemptAsset.asset_id)
                .where(
                    BuildNode.id.in_(deliverable_node_ids),
                    AttemptAsset.selected.is_(True),
                )
                .order_by(BuildNode.stable_key, AttemptAsset.role, AttemptAsset.ordinal)
            )
        ).all()
        if not asset_rows:
            raise ReleaseNotReadyError("Build has no selected deliverable assets.")
        release = Release(
            id=uuid.uuid4(),
            project_id=project.id,
            build_id=build.id,
            version_label=version_label,
            status=str(ReleaseStatus.DRAFT),
        )
        self._session.add(release)
        await self._session.flush()
        for stable_key, link, asset in asset_rows:
            if asset.verified_at is None or not await asyncio.to_thread(
                self._work_store.verify, asset.b2_key, expected_sha256=asset.sha256
            ):
                raise AssetVerificationError(
                    f"Selected release asset {asset.id} failed stored-byte verification."
                )
            filename = (asset.metadata_json or {}).get("filename")
            logical_name = (
                str(filename)
                if isinstance(filename, str)
                else f"{link.role}-{link.ordinal}.{_extension(asset.mime_type)}"
            )
            self._session.add(
                ReleaseAsset(
                    id=uuid.uuid4(),
                    release_id=release.id,
                    asset_id=asset.id,
                    logical_path=f"{stable_key}/{logical_name}",
                    role=link.role,
                )
            )
        assert_transition(ReleaseStatus.DRAFT, ReleaseStatus.READY_FOR_APPROVAL, subject="release")
        release.status = str(ReleaseStatus.READY_FOR_APPROVAL)
        self._event(project, build, "release.candidate_created", {"release_id": str(release.id)})
        self._audit(
            principal,
            project,
            "release.candidate_created",
            release.id,
            reason=f"version:{version_label}",
        )
        await self._session.flush()
        return await self._response(release)

    async def approve(
        self, *, release_id: uuid.UUID, principal: Principal, reason: str
    ) -> ReleaseResponse:
        release, project, build = await self._locked(release_id)
        self._authorize(principal, project)
        if release.status == str(ReleaseStatus.APPROVED):
            return await self._response(release)
        assert_transition(ReleaseStatus(release.status), ReleaseStatus.APPROVED, subject="release")
        release.status = str(ReleaseStatus.APPROVED)
        release.approved_by = principal.actor_id
        release.approved_at = datetime.now(UTC)
        self._session.add(
            Approval(
                id=uuid.uuid4(),
                project_id=project.id,
                target_type="RELEASE",
                target_id=release.id,
                decision="APPROVE",
                reason=reason,
                created_by=principal.actor_id,
            )
        )
        self._event(project, build, "release.approved", {"release_id": str(release.id)})
        self._audit(principal, project, "release.approved", release.id, reason=reason)
        await self._session.flush()
        return await self._response(release)

    async def publish(
        self, *, release_id: uuid.UUID, principal: Principal, reason: str
    ) -> ReleaseResponse:
        release, project, build = await self._locked(release_id)
        self._authorize(principal, project)
        if release.status == str(ReleaseStatus.PUBLISHED):
            return await self._response(release)
        assert_transition(
            ReleaseStatus(release.status), ReleaseStatus.PUBLISHING, subject="release"
        )
        release.status = str(ReleaseStatus.PUBLISHING)
        rows = (
            await self._session.execute(
                select(ReleaseAsset, Asset)
                .join(Asset, Asset.id == ReleaseAsset.asset_id)
                .where(ReleaseAsset.release_id == release.id)
                .order_by(ReleaseAsset.logical_path)
            )
        ).all()
        manifest_assets: list[JsonValue] = []
        for release_asset, asset in rows:
            raw = await asyncio.to_thread(self._work_store.get_bytes, asset.b2_key)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != asset.sha256:
                raise AssetVerificationError("Work asset changed before release publication.")
            key = release_key(
                organization_id=project.organization_id,
                project_id=project.id,
                release_id=release.id,
                logical_path=f"assets/{release_asset.logical_path}",
                prefix=self._release_store.prefix,
            )
            await asyncio.to_thread(
                self._release_store.store_bytes,
                key,
                raw,
                content_type=asset.mime_type,
                metadata={"sha256": digest, "release-id": str(release.id)},
            )
            if not await asyncio.to_thread(self._release_store.verify, key, expected_sha256=digest):
                raise AssetVerificationError("Published release asset failed readback.")
            manifest_assets.append(
                {
                    "logical_path": release_asset.logical_path,
                    "role": release_asset.role,
                    "sha256": digest,
                    "size_bytes": len(raw),
                    "mime_type": asset.mime_type,
                    "b2_key": key,
                }
            )
        manifest: JsonValue = {
            "schema": "takegraph.release_manifest.v1",
            "release_id": str(release.id),
            "project_id": str(project.id),
            "build_id": str(build.id),
            "version_label": release.version_label,
            "assets": manifest_assets,
        }
        manifest_bytes = canonical_bytes(manifest)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_key = release_key(
            organization_id=project.organization_id,
            project_id=project.id,
            release_id=release.id,
            logical_path="manifest.json",
            prefix=self._release_store.prefix,
        )
        await asyncio.to_thread(
            self._release_store.store_bytes,
            manifest_key,
            manifest_bytes,
            content_type="application/json",
            metadata={"sha256": manifest_sha, "release-id": str(release.id)},
        )
        verification: JsonValue = {
            "schema": "takegraph.release_verification.v1",
            "release_id": str(release.id),
            "manifest_sha256": manifest_sha,
            "checked_assets": len(manifest_assets),
            "all_hashes_verified": True,
            "retention_mode": "NOT_CONFIGURED",
        }
        verification_bytes = canonical_bytes(verification)
        verification_sha = hashlib.sha256(verification_bytes).hexdigest()
        verification_key = release_key(
            organization_id=project.organization_id,
            project_id=project.id,
            release_id=release.id,
            logical_path="verification.json",
            prefix=self._release_store.prefix,
        )
        await asyncio.to_thread(
            self._release_store.store_bytes,
            verification_key,
            verification_bytes,
            content_type="application/json",
            metadata={"sha256": verification_sha, "release-id": str(release.id)},
        )
        for key, digest in (
            (manifest_key, manifest_sha),
            (verification_key, verification_sha),
        ):
            if not await asyncio.to_thread(self._release_store.verify, key, expected_sha256=digest):
                raise AssetVerificationError("Release proof object failed readback.")
        release.manifest_asset_id = await self._index_proof_asset(
            project, manifest_key, manifest_bytes, "release_manifest"
        )
        release.verification_asset_id = await self._index_proof_asset(
            project, verification_key, verification_bytes, "release_verification"
        )
        release.retention_mode = "NOT_CONFIGURED"
        assert_transition(ReleaseStatus.PUBLISHING, ReleaseStatus.PUBLISHED, subject="release")
        release.status = str(ReleaseStatus.PUBLISHED)
        release.published_at = datetime.now(UTC)
        project.active_release_id = release.id
        project.version += 1
        self._event(
            project,
            build,
            "release.published",
            {
                "release_id": str(release.id),
                "version_label": release.version_label,
                "manifest_sha256": manifest_sha,
                "asset_count": len(manifest_assets),
                "retention_mode": release.retention_mode,
            },
        )
        self._audit(principal, project, "release.published", release.id, reason=reason)
        await self._session.flush()
        return await self._response(release)

    async def verify(self, *, release_id: uuid.UUID) -> VerificationResponse:
        release = await self._session.get(Release, release_id)
        if release is None or release.status != str(ReleaseStatus.PUBLISHED):
            raise ReleaseNotReadyError("Published release was not found.")
        project = await self._session.get(Project, release.project_id)
        if project is None or release.manifest_asset_id is None:
            raise ReleaseNotReadyError("Published release proof is incomplete.")
        manifest_asset = await self._session.get(Asset, release.manifest_asset_id)
        if manifest_asset is None:
            raise ReleaseNotReadyError("Release manifest asset is missing.")
        manifest = await asyncio.to_thread(self._release_store.get_bytes, manifest_asset.b2_key)
        manifest_sha = hashlib.sha256(manifest).hexdigest()
        if manifest_sha != manifest_asset.sha256:
            raise AssetVerificationError("Release manifest hash verification failed.")
        rows = (
            await self._session.execute(
                select(ReleaseAsset, Asset)
                .join(Asset, Asset.id == ReleaseAsset.asset_id)
                .where(ReleaseAsset.release_id == release.id)
            )
        ).all()
        # Concurrently, not one after another. Each check is a full download and
        # re-hash from object storage, so the sequential loop spent the sum of
        # every asset's round trip — 78 seconds for eight assets on a slow link,
        # which is long enough that a proxy in front of this gives up and the
        # caller is told the verification failed when it had not even finished.
        # The checks are independent; only the verdict is combined.
        async def _check(release_asset: ReleaseAsset, asset: Asset) -> str | None:
            key = release_key(
                organization_id=project.organization_id,
                project_id=project.id,
                release_id=release.id,
                logical_path=f"assets/{release_asset.logical_path}",
                prefix=self._release_store.prefix,
            )
            ok = await asyncio.to_thread(
                self._release_store.verify, key, expected_sha256=asset.sha256
            )
            return None if ok else release_asset.logical_path

        failures = [
            path
            for path in await asyncio.gather(*(_check(ra, a) for ra, a in rows))
            if path is not None
        ]
        if failures:
            # Name every asset that failed, not just the first. A partial answer
            # sends someone hunting one file when several may have drifted.
            raise AssetVerificationError(
                f"Release assets failed verification: {', '.join(sorted(failures))}."
            )
        return VerificationResponse(
            verified_at=datetime.now(UTC),
            release_id=release.id,
            verified=True,
            checked_assets=len(rows),
            manifest_sha256=manifest_sha,
            retention_mode=release.retention_mode or "NOT_CONFIGURED",
        )

    async def _index_proof_asset(
        self, project: Project, key: str, data: bytes, role: str
    ) -> uuid.UUID:
        digest = hashlib.sha256(data).hexdigest()
        asset_id = await self._session.scalar(
            insert(Asset)
            .values(
                id=uuid.uuid4(),
                organization_id=project.organization_id,
                sha256=digest,
                size_bytes=len(data),
                mime_type="application/json",
                media_kind="DOCUMENT",
                b2_bucket=self._release_store.bucket,
                b2_key=key,
                metadata_json={"role": role},
                verified_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[Asset.organization_id, Asset.sha256])
            .returning(Asset.id)
        )
        if asset_id is None:
            asset_id = await self._session.scalar(
                select(Asset.id).where(
                    Asset.organization_id == project.organization_id,
                    Asset.sha256 == digest,
                )
            )
        if asset_id is None:
            raise InvalidSourceError("Release proof asset could not be indexed.")
        return asset_id

    async def _locked(self, release_id: uuid.UUID) -> tuple[Release, Project, Build]:
        release = await self._session.scalar(
            select(Release).where(Release.id == release_id).with_for_update()
        )
        if release is None:
            raise NotFoundError("Release was not found.")
        project = await self._session.scalar(
            select(Project).where(Project.id == release.project_id).with_for_update()
        )
        build = await self._session.get(Build, release.build_id)
        if project is None or build is None:
            raise NotFoundError("Release project or build was not found.")
        return release, project, build

    @staticmethod
    def _authorize(principal: Principal, project: Project) -> None:
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.PUBLISH_RELEASE,
        )

    async def _response(self, release: Release) -> ReleaseResponse:
        count = len(
            (
                await self._session.scalars(
                    select(ReleaseAsset.id).where(ReleaseAsset.release_id == release.id)
                )
            ).all()
        )
        return ReleaseResponse(
            id=release.id,
            project_id=release.project_id,
            build_id=release.build_id,
            version_label=release.version_label,
            status=release.status,
            manifest_asset_id=release.manifest_asset_id,
            verification_asset_id=release.verification_asset_id,
            approved_by=release.approved_by,
            approved_at=release.approved_at,
            published_at=release.published_at,
            retention_mode=release.retention_mode,
            asset_count=count,
        )

    def _event(
        self, project: Project, build: Build, event_type: str, payload: dict[str, object]
    ) -> None:
        self._session.add(
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

    def _audit(
        self,
        principal: Principal,
        project: Project,
        action: str,
        target_id: uuid.UUID,
        *,
        reason: str,
    ) -> None:
        self._session.add(
            AuditLog(
                actor_user_id=principal.actor_id,
                actor_kind="SESSION",
                effective_role=str(principal.role),
                organization_id=project.organization_id,
                project_id=project.id,
                action=action,
                target_type="RELEASE",
                target_id=target_id,
                reason=reason,
                correlation_id=uuid.uuid4(),
            )
        )


def _extension(mime_type: str) -> str:
    return {
        "video/mp4": "mp4",
        "audio/wav": "wav",
        "image/png": "png",
        "image/jpeg": "jpg",
        "text/vtt": "vtt",
        "application/json": "json",
    }.get(mime_type, "bin")


__all__ = [
    "ApprovalRequest",
    "PublishRequest",
    "ReleaseResponse",
    "ReleaseService",
    "VerificationResponse",
]
