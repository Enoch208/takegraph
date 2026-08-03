"""Human review decisions on build nodes (PRD §11.6, §5.6, §16.6).

The system deliberately parks work it cannot decide alone — a failed identity
gate, an evaluator error, an ambiguous provider submission after a crash. §5.5
"Human authority remains final" makes that correct, but only if there is a way
for a human to actually decide. Without this endpoint a WAITING_REVIEW node
wedges its build permanently, which is a worse failure than the one that caused
it.

Three rules shape the module:

- §5.6 FR-QA-005: a manual decision records actor and reason, and the record is
  immutable and linked to the exact node. The reason is mandatory, not optional.
- §16.6: overriding an automatic failure or error requires a reason — so PASS on
  a node the system rejected is exactly where the justification matters most.
- §0.1 forbids auto-approving creative or legal output, so nothing here decides
  on its own; every transition needs an authenticated principal who holds
  APPROVE_VALIDATION.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import BuildNodeStatus, BuildStatus
from takegraph_domain.errors import InvalidSourceError, NotFoundError
from takegraph_domain.execution.idempotency import work_item_dedupe_key

from takegraph_api.auth import get_principal
from takegraph_api.db.models import (
    Approval,
    AuditLog,
    Build,
    BuildNode,
    DomainEvent,
    Project,
)
from takegraph_api.db.session import session_scope
from takegraph_api.queue import WorkQueue

router = APIRouter(prefix="/api/v1", tags=["review"])

#: What each decision does to the node. RETAKE returns it to the queue rather
#: than resolving it — §13.2 says an ambiguous submission may be retried by an
#: authorized user with an explicit new logical attempt slot, which is the usual
#: correct answer when a crash left a submission in doubt.
_TARGET = {
    "PASS": BuildNodeStatus.PASSED,
    "FAIL": BuildNodeStatus.FAILED,
    "RETAKE": BuildNodeStatus.RETAKE_PENDING,
}


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["PASS", "FAIL", "RETAKE"]
    reason: str = Field(min_length=3, max_length=500)
    """Mandatory. §16.6 requires a reason for overriding an automatic result, and
    a decision nobody can later explain is worth little as evidence."""


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_node_id: uuid.UUID
    stable_key: str
    decision: str
    status: str
    build_status: str
    approval_id: uuid.UUID


@router.post("/build-nodes/{build_node_id}/decision", response_model=DecisionResponse)
async def decide_build_node(
    build_node_id: uuid.UUID,
    body: DecisionRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> DecisionResponse:
    """Resolve a node waiting on human review (§11.6, owner/reviewer)."""
    async with session_scope() as session:
        node = await session.scalar(
            select(BuildNode).where(BuildNode.id == build_node_id).with_for_update()
        )
        if node is None:
            raise NotFoundError("Build node not found.")
        build = await session.scalar(
            select(Build).where(Build.id == node.build_id).with_for_update()
        )
        if build is None:
            raise NotFoundError("Build node not found.")
        project = await session.get(Project, build.project_id)
        if project is None:
            raise NotFoundError("Build node not found.")

        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.APPROVE_VALIDATION,
        )

        if node.status != str(BuildNodeStatus.WAITING_REVIEW):
            raise InvalidSourceError(
                f"{node.stable_key} is not awaiting review; it is {node.status}."
            )

        target = _TARGET[body.decision]

        # PASS means "accept the output that exists". A node with no selected
        # attempt has no output to accept, so approving it would mark a
        # dependency satisfiable by nothing — §5.4 FR-BUILD-007 exists to stop
        # exactly that. RETAKE is the honest resolution in that case.
        if target is BuildNodeStatus.PASSED and node.selected_attempt_id is None:
            raise InvalidSourceError(
                f"{node.stable_key} has no selected output to approve. "
                "Use RETAKE to run it again, or FAIL to stop the build."
            )

        assert_transition(BuildNodeStatus(node.status), target, subject="node")
        node.status = str(target)
        node.reason_code = f"HUMAN_{body.decision}"
        node.reason = body.reason
        node.version += 1
        if target in (BuildNodeStatus.PASSED, BuildNodeStatus.FAILED):
            node.completed_at = datetime.now(UTC)

        approval = Approval(
            id=uuid.uuid4(),
            project_id=project.id,
            target_type="BUILD_NODE",
            target_id=node.id,
            decision=body.decision,
            reason=body.reason,
            created_by=principal.actor_id,
        )
        # §19.8: the audit record is written in the same transaction as the
        # mutation it describes, so an approval can never exist without its trail.
        session.add_all(
            [
                approval,
                AuditLog(
                    actor_user_id=principal.actor_id,
                    actor_kind="SESSION",
                    effective_role=str(principal.role),
                    organization_id=project.organization_id,
                    project_id=project.id,
                    action=f"build_node.{body.decision.lower()}",
                    target_type="BUILD_NODE",
                    target_id=node.id,
                    before_ref=str(BuildNodeStatus.WAITING_REVIEW),
                    after_ref=str(target),
                    reason=body.reason,
                    correlation_id=uuid.uuid4(),
                ),
                DomainEvent(
                    event_id=uuid.uuid4(),
                    organization_id=project.organization_id,
                    project_id=project.id,
                    build_id=build.id,
                    event_type="build.node.decided",
                    payload_json={
                        "build_node_id": str(node.id),
                        "stable_key": node.stable_key,
                        "from": str(BuildNodeStatus.WAITING_REVIEW),
                        "to": str(target),
                        "decision": body.decision,
                        "reason": body.reason,
                        "actor_role": str(principal.role),
                    },
                    correlation_id=uuid.uuid4(),
                ),
            ]
        )

        if target is BuildNodeStatus.RETAKE_PENDING:
            # Requeue under a key that cannot collide with the submission that
            # went ambiguous, or the retake would be deduped away and the node
            # would sit in RETAKE_PENDING forever.
            await WorkQueue(session).enqueue(
                kind="EXECUTE_BUILD_NODE",
                target_id=node.id,
                build_id=build.id,
                dedupe_key=work_item_dedupe_key(
                    kind="EXECUTE_BUILD_NODE",
                    target_id=node.id,
                    discriminator=f"retake:{approval.id}",
                ),
                payload={"stable_key": node.stable_key, "trigger_source": "APPLICATION_COMMIT"},
            )

        # A build parked in WAITING_REVIEW has work to do again once a decision
        # unblocks it; §12.6 recomputes completion after every terminal node
        # transition, and the worker picks it up from RUNNING.
        if build.status == str(BuildStatus.WAITING_REVIEW) and target is not BuildNodeStatus.FAILED:
            assert_transition(BuildStatus.WAITING_REVIEW, BuildStatus.RUNNING, subject="build")
            build.status = str(BuildStatus.RUNNING)
            build.version += 1

        await session.flush()
        return DecisionResponse(
            build_node_id=node.id,
            stable_key=node.stable_key,
            decision=body.decision,
            status=node.status,
            build_status=build.status,
            approval_id=approval.id,
        )
