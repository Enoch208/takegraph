"""Tenant-scoped project, revision, source, and asset query services (PRD §11.4)."""

from __future__ import annotations

import copy
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.canonical import (
    CanonicalizationError,
    JsonValue,
    canonical_hash,
    canonical_payload,
)
from takegraph_domain.errors import (
    ForbiddenError,
    InvalidSourceError,
    NotFoundError,
    VersionConflictError,
)
from takegraph_infrastructure.b2 import B2Settings, B2Store

from takegraph_api.auth import require_permission
from takegraph_api.db.models import (
    Asset,
    Attempt,
    AttemptAsset,
    Build,
    BuildNode,
    Project,
    ProjectRevision,
    Release,
    ReleaseAsset,
    Source,
    SourceVersion,
)
from takegraph_api.db.session import session_scope

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    spec: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        if SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError("slug must contain lowercase words separated by hyphens")
        return value


class ProjectPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class ProjectResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    status: str
    active_revision_id: uuid.UUID | None
    active_release_id: uuid.UUID | None
    is_demo: bool
    version: int
    created_at: datetime


class RevisionResponse(BaseModel):
    id: uuid.UUID
    revision_no: int
    parent_revision_id: uuid.UUID | None
    canonical_hash: str
    spec: dict[str, Any]
    created_at: datetime


class SourceVersionResponse(BaseModel):
    id: uuid.UUID
    revision_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    content_hash: str
    mime_type: str | None
    media_kind: str | None
    size_bytes: int | None
    created_at: datetime


class SourceResponse(BaseModel):
    id: uuid.UUID
    stable_key: str
    kind: str
    versions: list[SourceVersionResponse]


class AssetAccessResponse(BaseModel):
    asset_id: uuid.UUID
    access_url: str
    expires_at: datetime


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, principal: Principal, request: ProjectCreateRequest
    ) -> ProjectResponse:
        project_id = uuid.uuid4()
        result = await self._session.execute(
            insert(Project)
            .values(
                id=project_id,
                organization_id=principal.organization_id,
                slug=request.slug,
                name=request.name,
                status="ACTIVE",
                is_demo=False,
                version=1,
            )
            .on_conflict_do_nothing(index_elements=[Project.organization_id, Project.slug])
            .returning(Project.id)
        )
        if result.scalar_one_or_none() is None:
            raise VersionConflictError("A project with this slug already exists.")

        spec = _project_spec(request.spec, name=request.name, status_value="ACTIVE")
        revision_id = uuid.uuid4()
        self._session.add(
            ProjectRevision(
                id=revision_id,
                project_id=project_id,
                revision_no=1,
                parent_revision_id=None,
                spec_json=spec,
                canonical_hash=canonical_hash(spec),
                created_by=principal.actor_id,
            )
        )
        await self._session.flush()
        await self._session.execute(
            update(Project).where(Project.id == project_id).values(active_revision_id=revision_id)
        )
        project = await self._session.get(Project, project_id)
        if project is None:
            raise NotFoundError("Created project could not be resolved.")
        return _project_response(project)

    async def list_projects(self, *, principal: Principal) -> list[ProjectResponse]:
        statement = (
            select(Project)
            .where(Project.organization_id == principal.organization_id)
            .order_by(Project.created_at, Project.id)
        )
        if principal.project_scope_id is not None:
            statement = statement.where(Project.id == principal.project_scope_id)
        rows = (await self._session.scalars(statement)).all()
        return [_project_response(project) for project in rows]

    async def get(self, *, project_id: uuid.UUID, principal: Principal) -> ProjectResponse:
        project = await self._project(project_id)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.VIEW_PROJECT,
        )
        return _project_response(project)

    async def patch(
        self,
        *,
        project_id: uuid.UUID,
        principal: Principal,
        expected_version: int,
        request: ProjectPatchRequest,
    ) -> ProjectResponse:
        project = await self._project(project_id, lock=True)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.MANAGE_PROJECT,
        )
        if project.version != expected_version:
            raise VersionConflictError(
                "Project version changed; reload before applying this update.",
                details={"current_version": project.version},
            )
        next_name = request.name if request.name is not None else project.name
        next_status = (
            ("ARCHIVED" if request.archived else "ACTIVE")
            if request.archived is not None
            else project.status
        )
        if next_name == project.name and next_status == project.status:
            return _project_response(project)

        parent = await self._active_revision(project)
        spec = _project_spec(parent.spec_json, name=next_name, status_value=next_status)
        revision_hash = canonical_hash(spec)
        existing_revision = await self._session.scalar(
            select(ProjectRevision).where(
                ProjectRevision.project_id == project.id,
                ProjectRevision.canonical_hash == revision_hash,
            )
        )
        if existing_revision is None:
            revision_id = uuid.uuid4()
            next_revision_no = (
                await self._session.scalar(
                    select(func.max(ProjectRevision.revision_no)).where(
                        ProjectRevision.project_id == project.id
                    )
                )
                or 0
            ) + 1
            self._session.add(
                ProjectRevision(
                    id=revision_id,
                    project_id=project.id,
                    revision_no=next_revision_no,
                    parent_revision_id=parent.id,
                    spec_json=spec,
                    canonical_hash=revision_hash,
                    created_by=principal.actor_id,
                )
            )
        else:
            # The schema deliberately makes canonical specs unique per project.
            # Reversing an archive/rename therefore points back to the exact
            # immutable revision rather than cloning identical bytes.
            revision_id = existing_revision.id
        project.name = next_name
        project.status = next_status
        project.active_revision_id = revision_id
        project.version += 1
        await self._session.flush()
        return _project_response(project)

    async def revisions(
        self, *, project_id: uuid.UUID, principal: Principal
    ) -> list[RevisionResponse]:
        project = await self._project(project_id)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.VIEW_PROJECT,
        )
        rows = (
            await self._session.scalars(
                select(ProjectRevision)
                .where(ProjectRevision.project_id == project.id)
                .order_by(ProjectRevision.revision_no.desc())
            )
        ).all()
        return [
            RevisionResponse(
                id=row.id,
                revision_no=row.revision_no,
                parent_revision_id=row.parent_revision_id,
                canonical_hash=row.canonical_hash,
                spec=row.spec_json,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def sources(self, *, project_id: uuid.UUID, principal: Principal) -> list[SourceResponse]:
        project = await self._project(project_id)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.VIEW_PROJECT,
        )
        sources = (
            await self._session.scalars(
                select(Source).where(Source.project_id == project.id).order_by(Source.stable_key)
            )
        ).all()
        response: list[SourceResponse] = []
        for source in sources:
            rows = (
                await self._session.execute(
                    select(SourceVersion, Asset)
                    .outerjoin(Asset, Asset.id == SourceVersion.asset_id)
                    .where(SourceVersion.source_id == source.id)
                    .order_by(SourceVersion.created_at.desc())
                )
            ).all()
            versions = [
                SourceVersionResponse(
                    id=source_version.id,
                    revision_id=source_version.revision_id,
                    asset_id=source_version.asset_id,
                    content_hash=source_version.content_hash,
                    mime_type=None if asset is None else asset.mime_type,
                    media_kind=None if asset is None else asset.media_kind,
                    size_bytes=None if asset is None else asset.size_bytes,
                    created_at=source_version.created_at,
                )
                for source_version, asset in rows
            ]
            response.append(
                SourceResponse(
                    id=source.id,
                    stable_key=source.stable_key,
                    kind=source.kind,
                    versions=versions,
                )
            )
        return response

    async def _project(self, project_id: uuid.UUID, *, lock: bool = False) -> Project:
        statement = select(Project).where(Project.id == project_id)
        if lock:
            statement = statement.with_for_update()
        project = await self._session.scalar(statement)
        if project is None:
            raise NotFoundError("Project was not found.")
        return project

    async def _active_revision(self, project: Project) -> ProjectRevision:
        if project.active_revision_id is None:
            raise InvalidSourceError("Project has no active revision.")
        revision = await self._session.get(ProjectRevision, project.active_revision_id)
        if revision is None:
            raise InvalidSourceError("Project active revision cannot be resolved.")
        return revision


class AssetAccessService:
    def __init__(self, session: AsyncSession, store: B2Store, *, ttl_seconds: int) -> None:
        self._session = session
        self._store = store
        self._ttl_seconds = ttl_seconds

    async def issue(self, *, asset_id: uuid.UUID, principal: Principal) -> AssetAccessResponse:
        asset = await self._session.get(Asset, asset_id)
        if asset is None:
            raise NotFoundError("Asset was not found.")
        if asset.organization_id != principal.organization_id:
            raise ForbiddenError("Asset is not available to this principal.")
        if principal.project_scope_id is not None:
            project_ids = await self._asset_project_ids(asset.id)
            if principal.project_scope_id not in project_ids:
                raise ForbiddenError("Asset is not available to this principal.")
        access_url = self._store.presign_get(asset.b2_key, ttl_seconds=self._ttl_seconds)
        return AssetAccessResponse(
            asset_id=asset.id,
            access_url=access_url,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._ttl_seconds),
        )

    async def _asset_project_ids(self, asset_id: uuid.UUID) -> set[uuid.UUID]:
        source_projects = await self._session.scalars(
            select(Source.project_id)
            .join(SourceVersion, SourceVersion.source_id == Source.id)
            .where(SourceVersion.asset_id == asset_id)
        )
        attempt_projects = await self._session.scalars(
            select(Build.project_id)
            .join(BuildNode, BuildNode.build_id == Build.id)
            .join(Attempt, Attempt.build_node_id == BuildNode.id)
            .join(AttemptAsset, AttemptAsset.attempt_id == Attempt.id)
            .where(AttemptAsset.asset_id == asset_id)
        )
        release_projects = await self._session.scalars(
            select(Build.project_id)
            .join(Release, Release.build_id == Build.id)
            .join(ReleaseAsset, ReleaseAsset.release_id == Release.id)
            .where(ReleaseAsset.asset_id == asset_id)
        )
        return set(source_projects).union(attempt_projects, release_projects)


def _project_spec(source: dict[str, Any], *, name: str, status_value: str) -> dict[str, JsonValue]:
    spec: dict[str, JsonValue] = copy.deepcopy(source)
    spec["project"] = {"name": name, "status": status_value}
    try:
        canonical_payload(spec)
    except CanonicalizationError as exc:
        raise InvalidSourceError(str(exc)) from exc
    return spec


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        organization_id=project.organization_id,
        slug=project.slug,
        name=project.name,
        status=project.status,
        active_revision_id=project.active_revision_id,
        active_release_id=project.active_release_id,
        is_demo=project.is_demo,
        version=project.version,
        created_at=project.created_at,
    )


def _if_match_version(value: str | None) -> int:
    if value is None:
        raise VersionConflictError("If-Match is required for project updates.")
    normalized = value.strip()
    if normalized.startswith('W/"') and normalized.endswith('"'):
        normalized = normalized[3:-1]
    elif normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    try:
        version = int(normalized)
    except ValueError as exc:
        raise VersionConflictError("If-Match must contain the current project version.") from exc
    if version <= 0:
        raise VersionConflictError("If-Match must contain the current project version.")
    return version


MemberPrincipal = Annotated[
    Principal,
    Depends(require_permission(Permission.VIEW_PROJECT)),
]
ManagerPrincipal = Annotated[
    Principal,
    Depends(require_permission(Permission.MANAGE_PROJECT)),
]
router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: ProjectCreateRequest,
    principal: ManagerPrincipal,
    response: Response,
) -> ProjectResponse:
    async with session_scope() as session:
        project = await ProjectService(session).create(principal=principal, request=request)
        response.headers["ETag"] = f'"{project.version}"'
        return project


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(principal: MemberPrincipal) -> list[ProjectResponse]:
    async with session_scope() as session:
        return await ProjectService(session).list_projects(principal=principal)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    principal: MemberPrincipal,
    response: Response,
) -> ProjectResponse:
    async with session_scope() as session:
        project = await ProjectService(session).get(project_id=project_id, principal=principal)
        response.headers["ETag"] = f'"{project.version}"'
        return project


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def patch_project(
    project_id: uuid.UUID,
    request: ProjectPatchRequest,
    principal: ManagerPrincipal,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ProjectResponse:
    async with session_scope() as session:
        project = await ProjectService(session).patch(
            project_id=project_id,
            principal=principal,
            expected_version=_if_match_version(if_match),
            request=request,
        )
        response.headers["ETag"] = f'"{project.version}"'
        return project


@router.get("/projects/{project_id}/revisions", response_model=list[RevisionResponse])
async def list_revisions(
    project_id: uuid.UUID,
    principal: MemberPrincipal,
) -> list[RevisionResponse]:
    async with session_scope() as session:
        return await ProjectService(session).revisions(project_id=project_id, principal=principal)


@router.get("/projects/{project_id}/sources", response_model=list[SourceResponse])
async def list_sources(
    project_id: uuid.UUID,
    principal: MemberPrincipal,
) -> list[SourceResponse]:
    async with session_scope() as session:
        return await ProjectService(session).sources(project_id=project_id, principal=principal)


@router.get("/assets/{asset_id}/access", response_model=AssetAccessResponse)
async def access_asset(
    asset_id: uuid.UUID,
    principal: MemberPrincipal,
) -> AssetAccessResponse:
    settings = B2Settings.from_env(dict(os.environ))
    store = B2Store(settings)
    try:
        async with session_scope() as session:
            ttl = int(os.environ.get("B2_SIGNED_URL_TTL_SECONDS", "900"))
            return await AssetAccessService(session, store, ttl_seconds=ttl).issue(
                asset_id=asset_id,
                principal=principal,
            )
    finally:
        store.close()
