"""Identity-neutral authorization policy (PRD §5.1).

Transport code may obtain a principal from an OIDC session, a bearer token, or a
scoped demo session. Permission decisions stay here so a hidden UI control can
never become the authorization boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from takegraph_domain.enums import Role
from takegraph_domain.errors import ForbiddenError


class Permission(StrEnum):
    VIEW_PROJECT = "VIEW_PROJECT"
    EDIT_SOURCES = "EDIT_SOURCES"
    EDIT_DEMO_DRAFT = "EDIT_DEMO_DRAFT"
    RUN_BUILD = "RUN_BUILD"
    RUN_DEMO_RETAKE = "RUN_DEMO_RETAKE"
    RETRY_NODE = "RETRY_NODE"
    APPROVE_VALIDATION = "APPROVE_VALIDATION"
    PUBLISH_RELEASE = "PUBLISH_RELEASE"
    MANAGE_PROJECT = "MANAGE_PROJECT"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.EDITOR: frozenset(
        {
            Permission.VIEW_PROJECT,
            Permission.EDIT_SOURCES,
            Permission.RUN_BUILD,
            Permission.RETRY_NODE,
            Permission.MANAGE_PROJECT,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Permission.VIEW_PROJECT,
            Permission.APPROVE_VALIDATION,
            Permission.PUBLISH_RELEASE,
        }
    ),
    Role.VIEWER: frozenset({Permission.VIEW_PROJECT}),
    Role.GUEST: frozenset(
        {
            Permission.VIEW_PROJECT,
            Permission.EDIT_DEMO_DRAFT,
            Permission.RUN_DEMO_RETAKE,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Principal:
    actor_id: uuid.UUID
    subject: str
    organization_id: uuid.UUID
    role: Role
    project_scope_id: uuid.UUID | None = None


def authorize_project(
    principal: Principal,
    *,
    project_id: uuid.UUID,
    project_organization_id: uuid.UUID,
    permission: Permission,
) -> None:
    """Enforce tenant, optional project scope, and the role matrix together.

    Every denial uses the same error so guessing another tenant's UUID cannot
    reveal whether that project exists.
    """
    if principal.organization_id != project_organization_id:
        raise ForbiddenError("Project is not available to this principal.")
    if principal.project_scope_id is not None and principal.project_scope_id != project_id:
        raise ForbiddenError("Project is not available to this principal.")
    if permission not in ROLE_PERMISSIONS[principal.role]:
        raise ForbiddenError("Principal is not allowed to perform this action.")
