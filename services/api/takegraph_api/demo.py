"""Public demo surface (PRD §11.3, UJ-01).

UJ-01's acceptance is blunt: "No blank dashboard, setup wizard, or login wall
blocks judging." So `POST /demo/session` takes no credentials and hands back a
short-lived token scoped to exactly one seeded project.

The scoping is real, not cosmetic. The token carries `role=GUEST` and
`project_scope_id`, and every downstream service re-checks both through
`authorize_project` — §5.1 FR-AUTH-003 requires permissions to be enforced in the
service layer rather than hidden in the UI. A guest who guesses another project's
UUID is refused with the same error as a non-existent one (§19.2), so probing
cannot reveal what exists.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.enums import Role
from takegraph_domain.errors import InvalidSourceError, NotFoundError
from takegraph_domain.execution.faults import FaultType, assert_injection_allowed
from takegraph_domain.graph.orbit import PARAM_BRIEF_TEXT, PARAM_LEGAL_LINE

from takegraph_api.auth import SessionClaims, get_principal, session_provider_from_env
from takegraph_api.db.models import (
    Asset,
    AttemptAsset,
    Build,
    BuildNode,
    Project,
    ProjectRevision,
    Release,
)
from takegraph_api.db.models import FaultRule as FaultRuleRow
from takegraph_api.db.session import session_scope

router = APIRouter(prefix="/api/v1", tags=["demo"])

DEFAULT_SESSION_TTL_SECONDS = 3600


class DemoSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 — an auth scheme name, not a credential
    issued_at: int
    expires_at: int
    project_id: uuid.UUID


class DemoProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    active_revision_id: uuid.UUID
    legal_line: str
    brief_text: str
    build_id: uuid.UUID
    build_status: str
    release_id: str
    release_version: str
    release_status: str


#: A selected asset only counts as real media when B2 verified it and it carries
#: a genuine media type. Development runs leave behind builds whose nodes
#: "succeeded" with 2 KB of application/octet-stream, and those are indistinguishable
#: from real ones by status alone.
_REAL_MEDIA_ASSETS = (
    select(
        BuildNode.build_id.label("build_id"),
        func.count().label("real_assets"),
    )
    .join(AttemptAsset, AttemptAsset.attempt_id == BuildNode.selected_attempt_id)
    .join(Asset, Asset.id == AttemptAsset.asset_id)
    .where(
        AttemptAsset.selected.is_(True),
        Asset.verified_at.is_not(None),
        Asset.mime_type != "application/octet-stream",
    )
    .group_by(BuildNode.build_id)
    .subquery()
)


async def _resolve_demo_project(session: AsyncSession) -> tuple[Project, Build]:
    """The demo project whose completed build is backed by real, verified media.

    Ordering by recency alone is not enough, and getting this wrong is not a
    cosmetic bug. A build can reach SUCCEEDED with every node holding a synthetic
    placeholder — that is exactly what §0.1 means by "unlabeled demo data on a
    path that appears live", and a judge would see a storyboard of grey BINARY
    tiles presented as a real production.

    So the selector requires verified assets with real media types, and ranks by
    how many of them a build has before falling back to recency.
    """
    row = (
        await session.execute(
            select(Project, Build)
            .join(Build, Build.project_id == Project.id)
            .join(_REAL_MEDIA_ASSETS, _REAL_MEDIA_ASSETS.c.build_id == Build.id)
            .where(
                Project.is_demo.is_(True),
                Build.status == "SUCCEEDED",
                Build.is_fixture.is_(False),
            )
            .order_by(_REAL_MEDIA_ASSETS.c.real_assets.desc(), Build.created_at.desc())
            .limit(1)
        )
    ).first()

    if row is None:
        raise NotFoundError(
            "No seeded demo project has a completed build backed by verified media. "
            "Run the seed against real providers to create one."
        )
    return row[0], row[1]


@router.post("/demo/session", response_model=DemoSessionResponse)
async def create_demo_session() -> DemoSessionResponse:
    """Issue a scoped guest session (§5.1 FR-AUTH-002).

    Short-lived and bound to one project. `nonce` makes each token unique so two
    concurrent visitors never share an identity, and `actor_id` is fresh per
    session so audit records attribute actions to a specific visit.
    """
    async with session_scope() as session:
        project, build = await _resolve_demo_project(session)

    ttl = int(os.environ.get("DEMO_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS))
    issued_at = int(time.time())
    expires_at = issued_at + ttl

    claims = SessionClaims(
        subject=f"guest:{secrets.token_hex(8)}",
        actor_id=uuid.uuid4(),
        organization_id=project.organization_id,
        role=Role.GUEST,
        project_scope_id=project.id,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=secrets.token_urlsafe(12),
    )

    return DemoSessionResponse(
        access_token=session_provider_from_env().issue(claims),
        issued_at=issued_at,
        expires_at=expires_at,
        project_id=build.project_id,
    )


@router.get("/demo/project", response_model=DemoProjectResponse)
async def get_demo_project(
    principal: Annotated[Principal, Depends(get_principal)],
) -> DemoProjectResponse:
    """Identifiers and intro state for the seeded project (§11.3).

    Returns the current legal line and brief straight from the active revision so
    the workspace never hard-codes the phrase the demo is about to change.
    """
    async with session_scope() as session:
        project, build = await _resolve_demo_project(session)

        # The token is scoped to a project, but the check still runs here — a
        # scope claim is an assertion, and §5.1 requires the service to verify it.
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.VIEW_PROJECT,
        )

        revision = await session.get(ProjectRevision, project.active_revision_id)
        if revision is None:
            raise NotFoundError("The demo project has no active revision.")

        parameters = dict(revision.spec_json.get("parameters", {}))

        release = await session.scalar(
            select(Release)
            .where(Release.project_id == project.id)
            .order_by(Release.created_at.desc())
            .limit(1)
        )

        return DemoProjectResponse(
            project_id=project.id,
            organization_id=project.organization_id,
            slug=project.slug,
            name=project.name,
            active_revision_id=revision.id,
            legal_line=str(parameters.get(PARAM_LEGAL_LINE, "")),
            brief_text=str(parameters.get(PARAM_BRIEF_TEXT, "")),
            build_id=build.id,
            build_status=build.status,
            # Empty strings rather than nulls keep the client types simple; the
            # UI treats "" as "no release yet" and shows nothing rather than a
            # broken link.
            release_id=str(release.id) if release else "",
            release_version=release.version_label if release else "",
            release_status=release.status if release else "",
        )


class FaultRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_stable_key: str
    fault_type: str = str(FaultType.PROVIDER_TIMEOUT)
    remaining_uses: int = 1
    ttl_seconds: int = 3600


class FaultRuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    node_stable_key: str
    fault_type: str
    remaining_uses: int
    expires_at: datetime


@router.post("/demo/fault-rules", response_model=FaultRuleResponse, status_code=201)
async def create_fault_rule(
    body: FaultRuleRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> FaultRuleResponse:
    """Arm a labelled, expiring failure rule (§11.3, §8.3.11).

    Owner/editor only — a guest can watch a recovery but must not be able to
    break the build for everyone else. `assert_injection_allowed` then requires
    both ALLOW_FAILURE_INJECTION and a demo-scoped project, so an operator cannot
    arm one against a real production by mistake.

    Rules expire and are consumed. §4.4 requires the resulting failure to be
    labelled TEST FAULT, which the worker does by setting
    `attempts.is_injected_fault`.
    """
    async with session_scope() as session:
        project, _ = await _resolve_demo_project(session)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.RUN_BUILD,
        )
        assert_injection_allowed(
            allow_failure_injection=os.environ.get("ALLOW_FAILURE_INJECTION", "").lower() == "true",
            project_is_demo=bool(project.is_demo),
        )

        try:
            fault_type = FaultType(body.fault_type)
        except ValueError as exc:
            raise InvalidSourceError(
                f"Unknown fault type {body.fault_type!r}. "
                f"Expected one of: {', '.join(sorted(f.value for f in FaultType))}."
            ) from exc

        if body.remaining_uses < 1:
            raise InvalidSourceError("remaining_uses must be at least 1.")

        expires_at = datetime.now(UTC) + timedelta(seconds=max(60, body.ttl_seconds))
        rule = FaultRuleRow(
            id=uuid.uuid4(),
            project_id=project.id,
            node_stable_key=body.node_stable_key,
            fault_type=str(fault_type),
            remaining_uses=body.remaining_uses,
            expires_at=expires_at,
            created_by=principal.actor_id,
        )
        session.add(rule)
        await session.flush()

        return FaultRuleResponse(
            id=rule.id,
            project_id=rule.project_id,
            node_stable_key=rule.node_stable_key,
            fault_type=rule.fault_type,
            remaining_uses=rule.remaining_uses,
            expires_at=expires_at,
        )
