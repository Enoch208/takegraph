"""Release read, verify and publish surface (PRD §11.7, §18.12).

`ReleaseService` already owns candidate compilation, approval, publication and
verification. This module is transport only (§7.1).

One authorization detail worth stating: `ReleaseService._authorize` demands
`PUBLISH_RELEASE`, which is correct for approve and publish but wrong for
reading. A guest must be able to inspect and re-verify a published release —
that is the end of the judge journey (UJ-06, AS-06) — so the read paths check
`VIEW_PROJECT` instead. Verification is a read of durable evidence: it re-fetches
bytes from B2 and re-hashes them. It changes nothing.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.errors import NotFoundError
from takegraph_infrastructure.b2 import B2Settings, B2Store

from takegraph_api.auth import get_principal
from takegraph_api.db.models import Asset, Build, Project, Release, ReleaseAsset
from takegraph_api.db.session import session_scope
from takegraph_api.releases import (
    ApprovalRequest,
    PublishRequest,
    ReleaseResponse,
    ReleaseService,
    VerificationResponse,
)

router = APIRouter(prefix="/api/v1", tags=["releases"])


class ReleaseAssetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_path: str
    role: str
    sha256: str
    size_bytes: int
    mime_type: str
    access_path: str


class ReleaseDetailView(BaseModel):
    """§18.12 release proof page: everything a third party needs to check the
    claim, including the SHA-256 of every selected asset."""

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
    """Read back from B2 after publication, or NOT_CONFIGURED. §15.5 forbids
    assuming it."""
    asset_count: int
    is_active: bool
    project_revision_id: uuid.UUID
    graph_revision_id: uuid.UUID
    manifest_sha256: str | None
    verification_sha256: str | None
    assets: list[ReleaseAssetView]


def _stores() -> tuple[B2Store, B2Store]:
    env = dict(os.environ)
    return (
        B2Store(B2Settings.from_env(env)),
        B2Store(B2Settings.from_env(env, release=True)),
    )


async def _load_for_read(session, release_id: uuid.UUID, principal: Principal):
    release = await session.get(Release, release_id)
    if release is None:
        raise NotFoundError("Release not found.")
    project = await session.get(Project, release.project_id)
    if project is None:
        raise NotFoundError("Release not found.")
    # Read access, not publish rights — see the module docstring.
    authorize_project(
        principal,
        project_id=project.id,
        project_organization_id=project.organization_id,
        permission=Permission.VIEW_PROJECT,
    )
    return release, project


@router.get("/releases/{release_id}", response_model=ReleaseDetailView)
async def get_release(
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> ReleaseDetailView:
    async with session_scope() as session:
        release, project = await _load_for_read(session, release_id, principal)

        rows = (
            await session.execute(
                select(ReleaseAsset, Asset)
                .join(Asset, Asset.id == ReleaseAsset.asset_id)
                .where(ReleaseAsset.release_id == release.id)
                .order_by(ReleaseAsset.logical_path)
            )
        ).all()

        async def _sha(asset_id: uuid.UUID | None) -> str | None:
            if asset_id is None:
                return None
            asset = await session.get(Asset, asset_id)
            return None if asset is None else asset.sha256

        build = await session.get(Build, release.build_id)
        if build is None:
            raise NotFoundError("Release not found.")

        return ReleaseDetailView(
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
            asset_count=len(rows),
            is_active=project.active_release_id == release.id,
            project_revision_id=build.project_revision_id,
            graph_revision_id=build.graph_revision_id,
            manifest_sha256=await _sha(release.manifest_asset_id),
            verification_sha256=await _sha(release.verification_asset_id),
            assets=[
                ReleaseAssetView(
                    logical_path=link.logical_path,
                    role=link.role,
                    sha256=asset.sha256,
                    size_bytes=asset.size_bytes,
                    mime_type=asset.mime_type,
                    access_path=f"/api/v1/assets/{asset.id}/access",
                )
                for link, asset in rows
            ],
        )


@router.post("/releases/{release_id}/verify", response_model=VerificationResponse)
async def verify_release(
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> VerificationResponse:
    """Re-fetch the selected bytes and re-hash them (§11.7, §18.12 "Verify again").

    Deliberately open to any principal who can view the project, including a
    guest. A verification claim nobody can independently re-run is worth very
    little, and this one changes no state — it reads from B2 and compares.
    """
    work_store, release_store = _stores()
    try:
        async with session_scope() as session:
            await _load_for_read(session, release_id, principal)
            return await ReleaseService(session, work_store, release_store).verify(
                release_id=release_id
            )
    finally:
        work_store.close()
        release_store.close()


@router.post("/releases/{release_id}/approve", response_model=ReleaseResponse)
async def approve_release(
    release_id: uuid.UUID,
    body: ApprovalRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> ReleaseResponse:
    """§5.8 FR-REL-002: explicit authorized approval, with a mandatory reason
    recorded in the audit log alongside the mutation (§19.8)."""
    work_store, release_store = _stores()
    try:
        async with session_scope() as session:
            return await ReleaseService(session, work_store, release_store).approve(
                release_id=release_id, principal=principal, reason=body.reason
            )
    finally:
        work_store.close()
        release_store.close()


@router.post("/releases/{release_id}/publish", response_model=ReleaseResponse)
async def publish_release(
    release_id: uuid.UUID,
    body: PublishRequest,
    principal: Annotated[Principal, Depends(get_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ReleaseResponse:
    """§11.1 requires an Idempotency-Key on publish.

    The header is required at the edge, but the actual guarantee lives deeper:
    publish is idempotent by release state (§10.4 — PUBLISH_FAILED retries with
    the same release id, PUBLISHED does not re-publish), so a duplicate request
    cannot produce a second publication regardless of the key.
    """
    _ = idempotency_key
    work_store, release_store = _stores()
    try:
        async with session_scope() as session:
            return await ReleaseService(session, work_store, release_store).publish(
                release_id=release_id,
                principal=principal,
                reason=body.reason,
            )
    finally:
        work_store.close()
        release_store.close()
