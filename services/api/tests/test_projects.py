"""Project/revision/source/asset API service tests (PRD §11.4)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from takegraph_api.db.models import Asset, Organization, Project, Source, SourceVersion
from takegraph_api.projects import (
    AssetAccessService,
    ProjectCreateRequest,
    ProjectPatchRequest,
    ProjectService,
    _if_match_version,
)
from takegraph_domain.auth import Principal
from takegraph_domain.enums import Role
from takegraph_domain.errors import ForbiddenError, InvalidSourceError, VersionConflictError


class StubStore:
    def __init__(self) -> None:
        self.requested: list[tuple[str, int]] = []

    def presign_get(self, key: str, *, ttl_seconds: int = 900) -> str:
        self.requested.append((key, ttl_seconds))
        return f"https://signed.invalid/{key}"


@pytest.fixture
def owner() -> Principal:
    return Principal(
        actor_id=uuid.uuid4(),
        subject="project-test-owner",
        organization_id=uuid.uuid4(),
        role=Role.OWNER,
    )


async def _add_organization(session, owner: Principal) -> None:
    session.add(
        Organization(
            id=owner.organization_id,
            slug=f"project-{uuid.uuid4().hex}",
            name="Project Tests",
        )
    )
    await session.commit()


async def _cleanup_organization(session, organization_id: uuid.UUID) -> None:
    await session.rollback()
    await session.execute(
        text(
            "delete from source_versions where source_id in "
            "(select sources.id from sources join projects on projects.id=sources.project_id "
            "where projects.organization_id=:organization_id)"
        ),
        {"organization_id": organization_id},
    )
    await session.execute(
        text(
            "delete from sources where project_id in "
            "(select id from projects where organization_id=:organization_id)"
        ),
        {"organization_id": organization_id},
    )
    await session.execute(
        text("update projects set active_revision_id=null where organization_id=:organization_id"),
        {"organization_id": organization_id},
    )
    await session.execute(
        text(
            "delete from graph_edges where graph_revision_id in "
            "(select graph_revisions.id from graph_revisions "
            "join project_revisions on project_revisions.id=graph_revisions.project_revision_id "
            "join projects on projects.id=project_revisions.project_id "
            "where projects.organization_id=:organization_id)"
        ),
        {"organization_id": organization_id},
    )
    await session.execute(
        text(
            "delete from graph_nodes where graph_revision_id in "
            "(select graph_revisions.id from graph_revisions "
            "join project_revisions on project_revisions.id=graph_revisions.project_revision_id "
            "join projects on projects.id=project_revisions.project_id "
            "where projects.organization_id=:organization_id)"
        ),
        {"organization_id": organization_id},
    )
    await session.execute(
        text(
            "delete from graph_revisions where project_revision_id in "
            "(select project_revisions.id from project_revisions "
            "join projects on projects.id=project_revisions.project_id "
            "where projects.organization_id=:organization_id)"
        ),
        {"organization_id": organization_id},
    )
    await session.execute(
        text(
            "delete from project_revisions where project_id in "
            "(select id from projects where organization_id=:organization_id)"
        ),
        {"organization_id": organization_id},
    )
    await session.execute(
        text("delete from projects where organization_id=:organization_id"),
        {"organization_id": organization_id},
    )
    await session.execute(
        text("delete from assets where organization_id=:organization_id"),
        {"organization_id": organization_id},
    )
    await session.execute(
        text("delete from organizations where id=:organization_id"),
        {"organization_id": organization_id},
    )
    await session.commit()


async def test_project_lifecycle_uses_versions_and_immutable_revisions(session, owner) -> None:
    await _add_organization(session, owner)
    service = ProjectService(session)
    try:
        created = await service.create(
            principal=owner,
            request=ProjectCreateRequest(
                slug="orbit-campaign",
                name="ORBIT",
                spec={"legal_line": "zero sugar"},
            ),
        )
        await session.commit()

        assert created.version == 1
        assert created.active_revision_id is not None
        assert len(await service.list_projects(principal=owner)) == 1
        assert (
            await session.scalar(
                text(
                    "select count(*) from graph_nodes where graph_revision_id in "
                    "(select id from graph_revisions where project_revision_id=:revision_id)"
                ),
                {"revision_id": created.active_revision_id},
            )
            == 18
        )
        assert await session.scalar(text("select count(*) from provider_policies")) == 5
        assert await session.scalar(text("select count(*) from validation_policies")) == 6

        archived = await service.patch(
            project_id=created.id,
            principal=owner,
            expected_version=1,
            request=ProjectPatchRequest(archived=True),
        )
        await session.commit()
        assert archived.status == "ARCHIVED"
        assert archived.version == 2

        restored = await service.patch(
            project_id=created.id,
            principal=owner,
            expected_version=2,
            request=ProjectPatchRequest(archived=False),
        )
        await session.commit()
        assert restored.status == "ACTIVE"
        assert restored.version == 3
        assert restored.active_revision_id == created.active_revision_id

        renamed = await service.patch(
            project_id=created.id,
            principal=owner,
            expected_version=3,
            request=ProjectPatchRequest(name="ORBIT Launch"),
        )
        await session.commit()
        assert renamed.name == "ORBIT Launch"
        assert renamed.version == 4

        revisions = await service.revisions(project_id=created.id, principal=owner)
        assert [revision.revision_no for revision in revisions] == [3, 2, 1]
        assert revisions[0].parent_revision_id == created.active_revision_id
        assert (
            await session.scalar(
                text(
                    "select count(*) from graph_revisions where project_revision_id in "
                    "(select id from project_revisions where project_id=:project_id)"
                ),
                {"project_id": created.id},
            )
            == 3
        )

        with pytest.raises(VersionConflictError):
            await service.patch(
                project_id=created.id,
                principal=owner,
                expected_version=1,
                request=ProjectPatchRequest(name="Stale update"),
            )
    finally:
        await _cleanup_organization(session, owner.organization_id)


async def test_project_create_rejects_float_in_hashed_spec(session, owner) -> None:
    await _add_organization(session, owner)
    try:
        with pytest.raises(InvalidSourceError, match="float"):
            await ProjectService(session).create(
                principal=owner,
                request=ProjectCreateRequest(
                    slug="unstable-spec",
                    name="Unstable",
                    spec={"cost": 1.5},
                ),
            )
        await session.rollback()
        assert (
            await session.scalar(
                select(Project.id).where(Project.organization_id == owner.organization_id)
            )
            is None
        )
    finally:
        await _cleanup_organization(session, owner.organization_id)


async def test_cross_tenant_project_read_is_denied(session, owner) -> None:
    await _add_organization(session, owner)
    try:
        project = await ProjectService(session).create(
            principal=owner,
            request=ProjectCreateRequest(slug="tenant-bound", name="Tenant Bound"),
        )
        await session.commit()
        outsider = Principal(uuid.uuid4(), "outsider", uuid.uuid4(), Role.OWNER)
        with pytest.raises(ForbiddenError):
            await ProjectService(session).get(project_id=project.id, principal=outsider)
    finally:
        await _cleanup_organization(session, owner.organization_id)


async def test_sources_include_history_and_asset_metadata(session, owner) -> None:
    await _add_organization(session, owner)
    try:
        project = await ProjectService(session).create(
            principal=owner,
            request=ProjectCreateRequest(slug="source-history", name="Source History"),
        )
        await session.flush()
        source = Source(
            id=uuid.uuid4(),
            project_id=project.id,
            stable_key="source.product_reference",
            kind="IMAGE",
        )
        asset = Asset(
            id=uuid.uuid4(),
            organization_id=owner.organization_id,
            sha256="a" * 64,
            size_bytes=128,
            mime_type="image/png",
            media_kind="IMAGE",
            b2_bucket="private-test",
            b2_key="tenants/test/source.png",
        )
        session.add_all([source, asset])
        await session.flush()
        session.add(
            SourceVersion(
                id=uuid.uuid4(),
                source_id=source.id,
                revision_id=project.active_revision_id,
                asset_id=asset.id,
                content_hash="a" * 64,
                created_by=owner.actor_id,
            )
        )
        await session.commit()

        response = await ProjectService(session).sources(project_id=project.id, principal=owner)
        assert len(response) == 1
        assert response[0].stable_key == "source.product_reference"
        assert response[0].versions[0].mime_type == "image/png"
        assert response[0].versions[0].size_bytes == 128
    finally:
        await _cleanup_organization(session, owner.organization_id)


async def test_asset_access_checks_guest_project_scope(session, owner) -> None:
    await _add_organization(session, owner)
    try:
        project = await ProjectService(session).create(
            principal=owner,
            request=ProjectCreateRequest(slug="asset-access", name="Asset Access"),
        )
        await session.flush()
        asset = Asset(
            id=uuid.uuid4(),
            organization_id=owner.organization_id,
            sha256="b" * 64,
            size_bytes=64,
            mime_type="image/png",
            media_kind="IMAGE",
            b2_bucket="private-test",
            b2_key="tenants/test/asset.png",
        )
        source = Source(
            id=uuid.uuid4(),
            project_id=project.id,
            stable_key="source.reference",
            kind="IMAGE",
        )
        session.add_all([asset, source])
        await session.flush()
        session.add(
            SourceVersion(
                id=uuid.uuid4(),
                source_id=source.id,
                revision_id=project.active_revision_id,
                asset_id=asset.id,
                content_hash="b" * 64,
                created_by=owner.actor_id,
            )
        )
        await session.commit()

        store = StubStore()
        guest = Principal(
            uuid.uuid4(),
            "guest",
            owner.organization_id,
            Role.GUEST,
            project_scope_id=project.id,
        )
        response = await AssetAccessService(session, store, ttl_seconds=120).issue(
            asset_id=asset.id,
            principal=guest,
        )
        assert response.access_url.endswith("tenants/test/asset.png")
        assert store.requested == [("tenants/test/asset.png", 120)]

        wrong_project = Principal(
            uuid.uuid4(),
            "wrong-project",
            owner.organization_id,
            Role.GUEST,
            project_scope_id=uuid.uuid4(),
        )
        with pytest.raises(ForbiddenError):
            await AssetAccessService(session, store, ttl_seconds=120).issue(
                asset_id=asset.id,
                principal=wrong_project,
            )
    finally:
        await _cleanup_organization(session, owner.organization_id)


@pytest.mark.parametrize(
    ("header", "expected"),
    [('"3"', 3), ('W/"4"', 4), ("5", 5)],
)
def test_if_match_parsing(header: str, expected: int) -> None:
    assert _if_match_version(header) == expected


@pytest.mark.parametrize("header", [None, "", '"bad"', '"0"'])
def test_if_match_rejects_missing_or_invalid_versions(header: str | None) -> None:
    with pytest.raises(VersionConflictError):
        _if_match_version(header)
