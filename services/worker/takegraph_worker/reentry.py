"""Re-entering a node that has already been attempted (PRD §13.2, §13.3, §5.5).

Three different things put a node back on the queue after an attempt exists, and
all three land in the same place in every handler:

- the recovery policy parked it (`RETRY_PENDING`, `FALLBACK_PENDING`),
- a human ordered a retake through the review endpoint (`RETAKE_PENDING`),
- nothing did, and this is simply the node's first submission.

`gmi_work` grew a correct version of the first case and the other four handlers
never did, so a failed Anthropic or ElevenLabs node parked itself for recovery
and then died with "attempt is in unsupported state" on the way back. Nothing
caught it because no node in the system had ever reached a second attempt. This
module is the one implementation all five handlers share.

It also owns `logical_attempt_slot`, which is the part of the idempotency key
(§13.2) that separates repeated submissions of the *same* logical work. Every
handler was passing the default of 0, which is correct only while a node never
submits twice with the same mechanism, provider and model. It does exactly that
on a same-model retry and on a repeated manual retake, and two such attempts
would derive an identical key and collide on `uq_attempts_idempotency_key`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_api.db.models import Attempt, BuildNode
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.enums import AttemptMechanism, AttemptStatus, BuildNodeStatus
from takegraph_domain.errors import InvalidSourceError

from takegraph_worker.recovery_work import plan_recovery

#: Statuses that mean "parked, and legitimately runnable again". Each has exactly
#: one outgoing edge to QUEUED in §10.2, which is the transition this module
#: performs.
_RECOVERY_PARKED = frozenset(
    {
        str(BuildNodeStatus.RETRY_PENDING),
        str(BuildNodeStatus.FALLBACK_PENDING),
    }
)
_RETAKE_PARKED = str(BuildNodeStatus.RETAKE_PENDING)

#: An attempt that is over. A retake is only safe once the previous submission
#: can no longer land — §13.2's whole concern is not paying twice for one piece
#: of work, and re-submitting alongside an in-flight attempt is exactly that.
_TERMINAL_ATTEMPTS = frozenset(
    {
        str(AttemptStatus.FAILED),
        str(AttemptStatus.TIMED_OUT),
        str(AttemptStatus.CANCELLED),
    }
)


@dataclass(frozen=True, slots=True)
class Reentry:
    """How a node that has already been attempted should submit this time."""

    mechanism: AttemptMechanism
    parent_attempt_id: uuid.UUID | None
    provider: str
    model: str
    timeout_seconds: int


async def plan_reentry(
    session: AsyncSession,
    *,
    node: BuildNode,
    latest: Attempt | None,
    provider: str,
    model: str,
    timeout_seconds: int,
    subject: str,
) -> Reentry | None:
    """Resolve a parked node into a submission plan, or None for a first attempt.

    Transitions the node to QUEUED as a side effect when it was parked, because
    QUEUED is the only status the handlers will submit from and §10.2 gives every
    parked status exactly that one outgoing edge.

    Recovery intent is re-derived from persisted state rather than read off the
    work-item payload: §6.3 makes PostgreSQL authoritative, and a payload can be
    stale or lost while the node's status and attempt history cannot.
    """
    status = node.status

    if status in _RECOVERY_PARKED:
        if latest is None or latest.status != str(AttemptStatus.FAILED):
            raise InvalidSourceError(
                f"{subject} is parked for recovery but has no failed attempt to recover from."
            )
        decision = await plan_recovery(
            session,
            node=node,
            error_class=latest.error_class or "INTERNAL",
            current_model=latest.model or model,
        )
        if not decision.should_retry:
            raise InvalidSourceError(
                f"{subject} is parked for recovery but the policy declines it: "
                f"{decision.reason_code}."
            )
        plan = Reentry(
            mechanism=decision.mechanism or AttemptMechanism.SAME_PROVIDER_RETRY,
            parent_attempt_id=latest.id,
            provider=decision.provider or provider,
            model=decision.model or model,
            timeout_seconds=decision.timeout_seconds or timeout_seconds,
        )
    elif status == _RETAKE_PARKED:
        # No policy consultation. A retake is a human overriding the system's own
        # conclusion (§5.5 "human authority remains final"), so the budget and
        # error-class rules that govern automatic recovery do not apply — the
        # person deciding has accepted the cost.
        if latest is None:
            raise InvalidSourceError(
                f"{subject} is parked for a retake but has never been attempted."
            )
        if latest.status not in _TERMINAL_ATTEMPTS:
            raise InvalidSourceError(
                f"{subject} cannot be retaken while attempt {latest.attempt_no} is "
                f"still {latest.status}; it must be resolved first."
            )
        plan = Reentry(
            mechanism=AttemptMechanism.MANUAL_RETRY,
            parent_attempt_id=latest.id,
            provider=provider,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    else:
        return None

    assert_transition(BuildNodeStatus(status), BuildNodeStatus.QUEUED, subject="node")
    node.status = str(BuildNodeStatus.QUEUED)
    return plan


async def logical_attempt_slot(
    session: AsyncSession,
    *,
    build_node_id: uuid.UUID,
    mechanism: AttemptMechanism,
    provider: str,
    model: str,
) -> int:
    """How many attempts already occupy this (mechanism, provider, model) triple.

    This is §13.2's `logical_attempt_slot`. The key must mean "the same billable
    work", so resuming an interrupted submission has to derive the same value —
    it does, because resuming reuses the existing attempt row and never reaches
    here. Deliberately starting another attempt with the same triple increments
    it, which is what makes a second same-model retry, or a second manual retake,
    a distinct submission rather than a unique-constraint violation.
    """
    return (
        await session.scalar(
            select(func.count(Attempt.id)).where(
                Attempt.build_node_id == build_node_id,
                Attempt.mechanism == str(mechanism),
                Attempt.provider == provider,
                Attempt.model == model,
            )
        )
        or 0
    )
