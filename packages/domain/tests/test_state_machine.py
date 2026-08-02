"""State machine tests — PRD §10 and §22.7's "all state-transition branches covered".

The transition tables are the safety rail that stops a failed node presenting as
passed. These tests check both directions: that every legal move the PRD draws is
permitted, and — more importantly — that the moves it does not draw are refused.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from takegraph_domain.builds.state_machine import (
    ATTEMPT_TRANSITIONS,
    BUILD_NODE_TRANSITIONS,
    BUILD_TRANSITIONS,
    RELEASE_TRANSITIONS,
    WORK_ITEM_TRANSITIONS,
    IllegalTransitionError,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_terminal,
)
from takegraph_domain.enums import (
    AttemptStatus,
    BuildNodeStatus,
    BuildStatus,
    ReleaseStatus,
    WorkItemStatus,
)

ALL_TABLES = [
    (BuildStatus, BUILD_TRANSITIONS),
    (BuildNodeStatus, BUILD_NODE_TRANSITIONS),
    (AttemptStatus, ATTEMPT_TRANSITIONS),
    (ReleaseStatus, RELEASE_TRANSITIONS),
    (WorkItemStatus, WORK_ITEM_TRANSITIONS),
]


class TestTableCompleteness:
    @pytest.mark.parametrize(
        ("enum_cls", "table"), ALL_TABLES, ids=lambda v: getattr(v, "__name__", "")
    )
    def test_every_state_has_an_entry(self, enum_cls, table) -> None:
        """A state missing from the table would raise KeyError at runtime, in the
        worker, mid-build. Better to catch it here."""
        assert set(table) == set(enum_cls), (
            f"missing: {sorted(str(s) for s in set(enum_cls) - set(table))}"
        )

    @pytest.mark.parametrize(
        ("enum_cls", "table"), ALL_TABLES, ids=lambda v: getattr(v, "__name__", "")
    )
    def test_targets_are_the_same_enum(self, enum_cls, table) -> None:
        for source, targets in table.items():
            for target in targets:
                assert isinstance(target, enum_cls), f"{source} -> {target} crosses enums"

    @pytest.mark.parametrize(
        ("enum_cls", "table"), ALL_TABLES, ids=lambda v: getattr(v, "__name__", "")
    )
    def test_no_state_transitions_to_itself(self, enum_cls, table) -> None:
        """A self-transition would let a retry loop appear to make progress."""
        for source, targets in table.items():
            assert source not in targets, f"{source} transitions to itself"

    @pytest.mark.parametrize(
        ("enum_cls", "table"), ALL_TABLES, ids=lambda v: getattr(v, "__name__", "")
    )
    def test_every_non_terminal_state_is_reachable(self, enum_cls, table) -> None:
        """An unreachable state is dead code that will confuse the UI's status
        rendering. Entry points are excluded — something has to start."""
        entry_points = {
            BuildStatus.PLANNED,
            BuildNodeStatus.PENDING,
            AttemptStatus.QUEUED,
            ReleaseStatus.DRAFT,
            WorkItemStatus.QUEUED,
        }
        reachable = {t for targets in table.values() for t in targets}
        for state in table:
            if state not in entry_points:
                assert state in reachable, f"{state} is unreachable"


class TestBuildStates:
    """§10.1"""

    def test_happy_path(self) -> None:
        assert can_transition(BuildStatus.PLANNED, BuildStatus.QUEUED)
        assert can_transition(BuildStatus.QUEUED, BuildStatus.RUNNING)
        assert can_transition(BuildStatus.RUNNING, BuildStatus.SUCCEEDED)

    def test_review_can_return_to_running(self) -> None:
        """A human decision unblocks the build rather than ending it."""
        assert can_transition(BuildStatus.WAITING_REVIEW, BuildStatus.RUNNING)

    def test_cannot_skip_straight_to_succeeded(self) -> None:
        assert not can_transition(BuildStatus.PLANNED, BuildStatus.SUCCEEDED)
        assert not can_transition(BuildStatus.QUEUED, BuildStatus.SUCCEEDED)

    def test_cancelling_only_reaches_cancelled(self) -> None:
        assert allowed_transitions(BuildStatus.CANCELLING) == frozenset({BuildStatus.CANCELLED})

    @pytest.mark.parametrize(
        "terminal", [BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED]
    )
    def test_terminal_states_are_terminal(self, terminal: BuildStatus) -> None:
        """§12.7: a failed build is not restarted in place. A resume creates a new
        build so history is preserved."""
        assert is_terminal(terminal)

    def test_failed_build_cannot_be_revived(self) -> None:
        with pytest.raises(IllegalTransitionError):
            assert_transition(BuildStatus.FAILED, BuildStatus.RUNNING, subject="build")


class TestBuildNodeStates:
    """§10.2"""

    def test_reuse_path_skips_execution(self) -> None:
        """A reused node never runs, stores or verifies — that is the entire point."""
        assert can_transition(BuildNodeStatus.PENDING, BuildNodeStatus.REUSED)
        assert is_terminal(BuildNodeStatus.REUSED)

    def test_generation_path_goes_through_storage_and_verification(self) -> None:
        for a, b in [
            (BuildNodeStatus.PENDING, BuildNodeStatus.QUEUED),
            (BuildNodeStatus.QUEUED, BuildNodeStatus.RUNNING),
            (BuildNodeStatus.RUNNING, BuildNodeStatus.STORING),
            (BuildNodeStatus.STORING, BuildNodeStatus.VERIFYING),
            (BuildNodeStatus.VERIFYING, BuildNodeStatus.PASSED),
        ]:
            assert can_transition(a, b), f"{a} -> {b} should be legal"

    def test_running_cannot_pass_without_storing(self) -> None:
        """§5.4 FR-BUILD-007: a provider URL alone cannot satisfy a dependency, so
        there is no edge from RUNNING to PASSED."""
        assert not can_transition(BuildNodeStatus.RUNNING, BuildNodeStatus.PASSED)

    def test_storing_cannot_pass_without_verifying(self) -> None:
        """Stored bytes still have to clear the quality gates."""
        assert not can_transition(BuildNodeStatus.STORING, BuildNodeStatus.PASSED)

    def test_recovery_paths_requeue(self) -> None:
        for pending in (
            BuildNodeStatus.RETRY_PENDING,
            BuildNodeStatus.FALLBACK_PENDING,
            BuildNodeStatus.RETAKE_PENDING,
        ):
            assert can_transition(pending, BuildNodeStatus.QUEUED)

    def test_review_resolves_both_ways(self) -> None:
        assert can_transition(BuildNodeStatus.WAITING_REVIEW, BuildNodeStatus.PASSED)
        assert can_transition(BuildNodeStatus.WAITING_REVIEW, BuildNodeStatus.FAILED)

    def test_only_passed_and_reused_satisfy_dependencies(self) -> None:
        """§12.6. This is the property the scheduler depends on; if any other
        status leaked in, a downstream node could consume unverified output."""
        satisfying = {s for s in BuildNodeStatus if s.satisfies_dependency}
        assert satisfying == {BuildNodeStatus.PASSED, BuildNodeStatus.REUSED}

    def test_waiting_review_does_not_satisfy_a_dependency(self) -> None:
        assert not BuildNodeStatus.WAITING_REVIEW.satisfies_dependency


class TestAttemptStates:
    """§10.3"""

    def test_full_submission_path(self) -> None:
        chain = [
            AttemptStatus.QUEUED,
            AttemptStatus.SUBMITTING,
            AttemptStatus.SUBMITTED,
            AttemptStatus.POLLING,
            AttemptStatus.FETCHING,
            AttemptStatus.STORED,
            AttemptStatus.SUCCEEDED,
        ]
        for a, b in pairwise(chain):
            assert can_transition(a, b), f"{a} -> {b} should be legal"

    def test_success_is_only_reachable_through_stored(self) -> None:
        """§10.3: "An attempt is not SUCCEEDED until required bytes are stored and
        hashed." Any other edge into SUCCEEDED would let a provider's word stand
        in for durable bytes."""
        sources = [
            s for s, targets in ATTEMPT_TRANSITIONS.items() if AttemptStatus.SUCCEEDED in targets
        ]
        assert sources == [AttemptStatus.STORED]

    def test_polling_cannot_jump_to_stored(self) -> None:
        assert not can_transition(AttemptStatus.POLLING, AttemptStatus.STORED)

    @pytest.mark.parametrize(
        "state",
        [
            AttemptStatus.QUEUED,
            AttemptStatus.SUBMITTING,
            AttemptStatus.SUBMITTED,
            AttemptStatus.POLLING,
            AttemptStatus.FETCHING,
        ],
    )
    def test_every_in_flight_state_can_fail(self, state: AttemptStatus) -> None:
        """A provider can die at any point; no state may be a trap."""
        assert can_transition(state, AttemptStatus.FAILED)
        assert can_transition(state, AttemptStatus.TIMED_OUT)

    def test_stored_can_still_fail(self) -> None:
        """Bytes landed but hash verification can still reject them (§8.3.7)."""
        assert can_transition(AttemptStatus.STORED, AttemptStatus.FAILED)


class TestReleaseStates:
    """§10.4"""

    def test_publish_requires_approval_first(self) -> None:
        """§5.8 FR-REL-002: "No automatic publish after generation.""" ""
        assert not can_transition(ReleaseStatus.DRAFT, ReleaseStatus.PUBLISHING)
        assert not can_transition(ReleaseStatus.READY_FOR_APPROVAL, ReleaseStatus.PUBLISHING)
        assert can_transition(ReleaseStatus.APPROVED, ReleaseStatus.PUBLISHING)

    def test_publishing_is_only_reachable_from_approved_or_a_failed_publish(self) -> None:
        sources = {s for s, t in RELEASE_TRANSITIONS.items() if ReleaseStatus.PUBLISHING in t}
        assert sources == {ReleaseStatus.APPROVED, ReleaseStatus.PUBLISH_FAILED}

    def test_publish_failure_is_retryable(self) -> None:
        """§10.4: retryable with the same release ID and idempotency key."""
        assert can_transition(ReleaseStatus.PUBLISH_FAILED, ReleaseStatus.PUBLISHING)

    def test_superseded_release_is_restorable(self) -> None:
        """§10.4 and UJ-07: restoring flips the project's active pointer. The
        historical release object itself is never mutated (§8.3.9)."""
        assert can_transition(ReleaseStatus.SUPERSEDED, ReleaseStatus.PUBLISHED)

    def test_published_cannot_be_rejected(self) -> None:
        """Rejecting after publication would rewrite history that a third party
        may already have verified."""
        assert not can_transition(ReleaseStatus.PUBLISHED, ReleaseStatus.REJECTED)


class TestWorkItemStates:
    """§10.5"""

    def test_lease_expiry_returns_the_item_to_the_queue(self) -> None:
        assert can_transition(WorkItemStatus.LEASED, WorkItemStatus.QUEUED)

    def test_leased_can_retry_or_die(self) -> None:
        assert can_transition(WorkItemStatus.LEASED, WorkItemStatus.RETRY_WAIT)
        assert can_transition(WorkItemStatus.LEASED, WorkItemStatus.DEAD)

    def test_done_is_terminal(self) -> None:
        assert is_terminal(WorkItemStatus.DONE)


class TestAssertTransition:
    def test_legal_transition_is_silent(self) -> None:
        assert_transition(BuildStatus.PLANNED, BuildStatus.QUEUED, subject="build")

    def test_illegal_transition_names_both_states_and_the_legal_set(self) -> None:
        """The usual cause is two workers racing on one node, so the message has
        to say which state the loser actually found."""
        with pytest.raises(IllegalTransitionError) as excinfo:
            assert_transition(BuildNodeStatus.PASSED, BuildNodeStatus.RUNNING, subject="node")

        message = str(excinfo.value)
        assert "PASSED" in message
        assert "RUNNING" in message
        assert "terminal" in message
        assert excinfo.value.details["from"] == "PASSED"

    def test_mixing_enums_is_a_type_error_not_a_silent_false(self) -> None:
        with pytest.raises(TypeError):
            can_transition(BuildStatus.RUNNING, AttemptStatus.QUEUED)
