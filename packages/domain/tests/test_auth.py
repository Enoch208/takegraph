from __future__ import annotations

import uuid

import pytest
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.enums import Role
from takegraph_domain.errors import ForbiddenError

ORG = uuid.UUID("10000000-0000-0000-0000-000000000001")
PROJECT = uuid.UUID("20000000-0000-0000-0000-000000000002")


@pytest.mark.parametrize("role", [Role.OWNER, Role.EDITOR])
def test_owner_and_editor_can_edit_sources(role: Role) -> None:
    authorize_project(
        Principal(uuid.uuid4(), "member", ORG, role),
        project_id=PROJECT,
        project_organization_id=ORG,
        permission=Permission.EDIT_SOURCES,
    )


@pytest.mark.parametrize("role", [Role.REVIEWER, Role.VIEWER, Role.GUEST])
def test_non_editors_cannot_edit_sources(role: Role) -> None:
    with pytest.raises(ForbiddenError):
        authorize_project(
            Principal(uuid.uuid4(), "member", ORG, role),
            project_id=PROJECT,
            project_organization_id=ORG,
            permission=Permission.EDIT_SOURCES,
        )


def test_cross_tenant_project_is_denied_without_revealing_existence() -> None:
    with pytest.raises(ForbiddenError, match="not available"):
        authorize_project(
            Principal(uuid.uuid4(), "owner", uuid.uuid4(), Role.OWNER),
            project_id=PROJECT,
            project_organization_id=ORG,
            permission=Permission.VIEW_PROJECT,
        )


def test_guest_is_confined_to_its_project() -> None:
    principal = Principal(uuid.uuid4(), "guest", ORG, Role.GUEST, PROJECT)
    authorize_project(
        principal,
        project_id=PROJECT,
        project_organization_id=ORG,
        permission=Permission.VIEW_PROJECT,
    )
    with pytest.raises(ForbiddenError):
        authorize_project(
            principal,
            project_id=uuid.uuid4(),
            project_organization_id=ORG,
            permission=Permission.VIEW_PROJECT,
        )


def test_guest_demo_permissions_do_not_grant_general_build_or_source_mutation() -> None:
    principal = Principal(uuid.uuid4(), "guest", ORG, Role.GUEST, PROJECT)
    for permission in (Permission.EDIT_SOURCES, Permission.RUN_BUILD):
        with pytest.raises(ForbiddenError):
            authorize_project(
                principal,
                project_id=PROJECT,
                project_organization_id=ORG,
                permission=permission,
            )
