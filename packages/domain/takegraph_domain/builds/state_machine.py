"""State machines for builds, nodes, attempts and releases (PRD §10).

§10 opens with a rule that shapes this module: "Transitions must be centralized
in domain services... Route handlers may request transitions but cannot set
status directly." So every legal transition in the system is declared here, once,
as data — and `assert_transition` is the only way to move.

Declaring transitions as a table rather than scattering `if status ==` checks
means an illegal move is impossible to write by accident, and the allowed set is
reviewable against the PRD diagrams side by side.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from takegraph_domain.enums import (
    AttemptStatus,
    BuildNodeStatus,
    BuildStatus,
    ReleaseStatus,
    WorkItemStatus,
)
from takegraph_domain.errors import DomainError


class IllegalTransitionError(DomainError):
    """A transition that the state machine does not permit.

    Raised rather than tolerated: silently ignoring an illegal move would let a
    failed node appear passed, which §0.1 forbids ("swallow errors or convert
    failed builds into successful UI states").
    """


# §10.1 Build states.
BUILD_TRANSITIONS: dict[BuildStatus, frozenset[BuildStatus]] = {
    BuildStatus.PLANNED: frozenset({BuildStatus.QUEUED, BuildStatus.CANCELLED}),
    BuildStatus.QUEUED: frozenset({BuildStatus.RUNNING, BuildStatus.CANCELLED}),
    BuildStatus.RUNNING: frozenset(
        {
            BuildStatus.WAITING_REVIEW,
            BuildStatus.SUCCEEDED,
            BuildStatus.FAILED,
            BuildStatus.CANCELLING,
        }
    ),
    # A human decision can unblock the build and return it to work.
    BuildStatus.WAITING_REVIEW: frozenset(
        {BuildStatus.RUNNING, BuildStatus.FAILED, BuildStatus.CANCELLING}
    ),
    BuildStatus.CANCELLING: frozenset({BuildStatus.CANCELLED}),
    # Terminal. §10.1: a failed or cancelled build is not resumed in place — a
    # new resume build is created instead (§12.7), preserving history.
    BuildStatus.SUCCEEDED: frozenset(),
    BuildStatus.FAILED: frozenset(),
    BuildStatus.CANCELLED: frozenset(),
}

# §10.2 Build-node states.
BUILD_NODE_TRANSITIONS: dict[BuildNodeStatus, frozenset[BuildNodeStatus]] = {
    BuildNodeStatus.PENDING: frozenset(
        {BuildNodeStatus.REUSED, BuildNodeStatus.QUEUED, BuildNodeStatus.CANCELLED}
    ),
    BuildNodeStatus.QUEUED: frozenset({BuildNodeStatus.RUNNING, BuildNodeStatus.CANCELLED}),
    BuildNodeStatus.RUNNING: frozenset(
        {
            BuildNodeStatus.STORING,
            BuildNodeStatus.RETRY_PENDING,
            BuildNodeStatus.FALLBACK_PENDING,
            BuildNodeStatus.FAILED,
            BuildNodeStatus.CANCELLED,
        }
    ),
    # §5.4 FR-BUILD-007: storage happens before validation, and a provider URL
    # alone can never satisfy a dependency.
    BuildNodeStatus.STORING: frozenset(
        {BuildNodeStatus.VERIFYING, BuildNodeStatus.FAILED, BuildNodeStatus.CANCELLED}
    ),
    BuildNodeStatus.VERIFYING: frozenset(
        {
            BuildNodeStatus.PASSED,
            BuildNodeStatus.WAITING_REVIEW,
            BuildNodeStatus.RETAKE_PENDING,
            BuildNodeStatus.FAILED,
            BuildNodeStatus.CANCELLED,
        }
    ),
    # §5.6 FR-QA-005: a human decision resolves this, and it is never resolved
    # automatically in either direction.
    BuildNodeStatus.WAITING_REVIEW: frozenset(
        {BuildNodeStatus.PASSED, BuildNodeStatus.FAILED, BuildNodeStatus.RETAKE_PENDING}
    ),
    BuildNodeStatus.RETRY_PENDING: frozenset(
        {BuildNodeStatus.QUEUED, BuildNodeStatus.FAILED, BuildNodeStatus.CANCELLED}
    ),
    BuildNodeStatus.FALLBACK_PENDING: frozenset(
        {BuildNodeStatus.QUEUED, BuildNodeStatus.FAILED, BuildNodeStatus.CANCELLED}
    ),
    BuildNodeStatus.RETAKE_PENDING: frozenset(
        {BuildNodeStatus.QUEUED, BuildNodeStatus.FAILED, BuildNodeStatus.CANCELLED}
    ),
    # Terminal for this build.
    BuildNodeStatus.PASSED: frozenset(),
    BuildNodeStatus.REUSED: frozenset(),
    BuildNodeStatus.FAILED: frozenset(),
    BuildNodeStatus.CANCELLED: frozenset(),
}

# §10.3 Attempt states.
_ATTEMPT_ABORTS = frozenset(
    {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT, AttemptStatus.CANCELLED}
)
ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.QUEUED: frozenset({AttemptStatus.SUBMITTING}) | _ATTEMPT_ABORTS,
    AttemptStatus.SUBMITTING: frozenset({AttemptStatus.SUBMITTED}) | _ATTEMPT_ABORTS,
    AttemptStatus.SUBMITTED: frozenset({AttemptStatus.POLLING}) | _ATTEMPT_ABORTS,
    AttemptStatus.POLLING: frozenset({AttemptStatus.FETCHING}) | _ATTEMPT_ABORTS,
    AttemptStatus.FETCHING: frozenset({AttemptStatus.STORED}) | _ATTEMPT_ABORTS,
    # §10.3: "An attempt is not SUCCEEDED until required bytes are stored and
    # hashed." STORED is that gate, and SUCCEEDED is only reachable through it.
    AttemptStatus.STORED: frozenset({AttemptStatus.SUCCEEDED, AttemptStatus.FAILED}),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
}

# §10.4 Release states.
RELEASE_TRANSITIONS: dict[ReleaseStatus, frozenset[ReleaseStatus]] = {
    ReleaseStatus.DRAFT: frozenset({ReleaseStatus.READY_FOR_APPROVAL, ReleaseStatus.REJECTED}),
    ReleaseStatus.READY_FOR_APPROVAL: frozenset({ReleaseStatus.APPROVED, ReleaseStatus.REJECTED}),
    # §5.8 FR-REL-002: publication follows an explicit authorized approval. There
    # is no edge from DRAFT or READY_FOR_APPROVAL straight to PUBLISHING.
    ReleaseStatus.APPROVED: frozenset({ReleaseStatus.PUBLISHING, ReleaseStatus.REJECTED}),
    ReleaseStatus.PUBLISHING: frozenset({ReleaseStatus.PUBLISHED, ReleaseStatus.PUBLISH_FAILED}),
    # §10.4: PUBLISH_FAILED is retryable with the same release ID and idempotency
    # key provided the evidence did not change.
    ReleaseStatus.PUBLISH_FAILED: frozenset({ReleaseStatus.PUBLISHING, ReleaseStatus.REJECTED}),
    ReleaseStatus.PUBLISHED: frozenset({ReleaseStatus.SUPERSEDED}),
    # §10.4: "SUPERSEDED remains valid and restorable." Restoring flips the
    # project's active pointer; the historical release object is never mutated
    # (§8.3.9), which is why this returns to PUBLISHED rather than re-publishing.
    ReleaseStatus.SUPERSEDED: frozenset({ReleaseStatus.PUBLISHED}),
    ReleaseStatus.REJECTED: frozenset(),
}

# §10.5 Work-item lease states.
WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.QUEUED: frozenset({WorkItemStatus.LEASED, WorkItemStatus.CANCELLED}),
    WorkItemStatus.LEASED: frozenset(
        {
            WorkItemStatus.DONE,
            WorkItemStatus.RETRY_WAIT,
            WorkItemStatus.DEAD,
            # Lease expiry returns the item to the queue without a worker acting.
            WorkItemStatus.QUEUED,
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.RETRY_WAIT: frozenset(
        {WorkItemStatus.LEASED, WorkItemStatus.DEAD, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.DONE: frozenset(),
    WorkItemStatus.DEAD: frozenset(),
    WorkItemStatus.CANCELLED: frozenset(),
}

_TABLES: dict[type[StrEnum], dict[Any, frozenset[Any]]] = {
    BuildStatus: BUILD_TRANSITIONS,
    BuildNodeStatus: BUILD_NODE_TRANSITIONS,
    AttemptStatus: ATTEMPT_TRANSITIONS,
    ReleaseStatus: RELEASE_TRANSITIONS,
    WorkItemStatus: WORK_ITEM_TRANSITIONS,
}


def allowed_transitions[S: StrEnum](current: S) -> frozenset[S]:
    table = _TABLES.get(type(current))
    if table is None:
        raise TypeError(f"no transition table for {type(current).__name__}")
    return cast("frozenset[S]", table[current])


def can_transition[S: StrEnum](current: S, target: S) -> bool:
    if type(current) is not type(target):
        raise TypeError(f"cannot compare {type(current).__name__} with {type(target).__name__}")
    return target in allowed_transitions(current)


def assert_transition[S: StrEnum](current: S, target: S, *, subject: str = "entity") -> None:
    """Raise unless the move is legal. The single gate every transition passes.

    The error names both states and what was legal, because the usual cause is a
    race — two workers acting on one node — and the diagnostic needs to say which
    state the loser actually found.
    """
    if not can_transition(current, target):
        legal = sorted(str(s) for s in allowed_transitions(current))
        raise IllegalTransitionError(
            f"{subject} cannot move {current} -> {target}; "
            f"legal from {current}: {', '.join(legal) or '(terminal)'}",
            details={"from": str(current), "to": str(target), "allowed": legal},
        )


def is_terminal[S: StrEnum](current: S) -> bool:
    return not allowed_transitions(current)
