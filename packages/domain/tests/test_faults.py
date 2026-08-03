"""Fault injection guard tests — PRD §8.3.11, §4.4.

Failure injection is the one feature in the system whose whole purpose is to
break a build. The tests that matter are the ones proving it cannot fire where it
should not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from takegraph_domain.enums import ErrorClass
from takegraph_domain.execution.faults import (
    FaultInjectionForbiddenError,
    FaultRule,
    FaultType,
    InjectedFault,
    assert_injection_allowed,
    select_fault,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PROJECT = uuid.uuid4()


def rule(**kw) -> FaultRule:
    base = {
        "id": uuid.uuid4(),
        "project_id": PROJECT,
        "node_stable_key": "video.clip.03",
        "fault_type": FaultType.PROVIDER_TIMEOUT,
        "remaining_uses": 1,
        "expires_at": NOW + timedelta(hours=1),
    }
    return FaultRule(**{**base, **kw})


class TestCreationGuards:
    """§8.3.11 requires ALLOW_FAILURE_INJECTION *and* a demo-scoped project."""

    def test_allowed_when_both_conditions_hold(self) -> None:
        assert_injection_allowed(allow_failure_injection=True, project_is_demo=True)

    def test_refused_without_the_flag(self) -> None:
        with pytest.raises(FaultInjectionForbiddenError, match="ALLOW_FAILURE_INJECTION"):
            assert_injection_allowed(allow_failure_injection=False, project_is_demo=True)

    def test_refused_on_a_non_demo_project(self) -> None:
        """The flag alone is not enough. A staging environment with the flag on
        must still not be able to break a real customer project."""
        with pytest.raises(FaultInjectionForbiddenError, match="demo or test"):
            assert_injection_allowed(allow_failure_injection=True, project_is_demo=False)

    def test_refused_when_neither_holds(self) -> None:
        with pytest.raises(FaultInjectionForbiddenError):
            assert_injection_allowed(allow_failure_injection=False, project_is_demo=False)


class TestSelection:
    def test_matches_the_target_node(self) -> None:
        found = select_fault(
            [rule()],
            stable_key="video.clip.03",
            now=NOW,
            allow_failure_injection=True,
            project_is_demo=True,
        )
        assert found is not None

    def test_does_not_match_another_node(self) -> None:
        """A rule armed for clip 3 must not break clip 1."""
        assert (
            select_fault(
                [rule()],
                stable_key="video.clip.01",
                now=NOW,
                allow_failure_injection=True,
                project_is_demo=True,
            )
            is None
        )

    def test_exhausted_rule_does_not_fire(self) -> None:
        """A rule that stayed armed after firing would leave the demo permanently
        broken rather than demonstrating a recovery."""
        assert (
            select_fault(
                [rule(remaining_uses=0)],
                stable_key="video.clip.03",
                now=NOW,
                allow_failure_injection=True,
                project_is_demo=True,
            )
            is None
        )

    def test_expired_rule_does_not_fire(self) -> None:
        assert (
            select_fault(
                [rule(expires_at=NOW - timedelta(seconds=1))],
                stable_key="video.clip.03",
                now=NOW,
                allow_failure_injection=True,
                project_is_demo=True,
            )
            is None
        )

    def test_rule_without_expiry_stays_armed(self) -> None:
        found = select_fault(
            [rule(expires_at=None)],
            stable_key="video.clip.03",
            now=NOW,
            allow_failure_injection=True,
            project_is_demo=True,
        )
        assert found is not None

    def test_disabled_flag_silently_ignores_stale_rules(self) -> None:
        """Returns None rather than raising: a leftover rule must not break an
        ordinary build. Only an explicit request to create one is refused loudly."""
        assert (
            select_fault(
                [rule()],
                stable_key="video.clip.03",
                now=NOW,
                allow_failure_injection=False,
                project_is_demo=True,
            )
            is None
        )

    def test_non_demo_project_ignores_rules(self) -> None:
        assert (
            select_fault(
                [rule()],
                stable_key="video.clip.03",
                now=NOW,
                allow_failure_injection=True,
                project_is_demo=False,
            )
            is None
        )


class TestFaultSemantics:
    """The injected fault must read to the recovery policy the same way the real
    failure it stands in for would."""

    @pytest.mark.parametrize(
        ("fault", "expected"),
        [
            (FaultType.PROVIDER_TIMEOUT, ErrorClass.TRANSIENT),
            (FaultType.PROVIDER_ERROR, ErrorClass.MODEL),
            (FaultType.PROVIDER_QUOTA, ErrorClass.QUOTA),
            (FaultType.STORAGE_FAILURE, ErrorClass.STORAGE),
        ],
    )
    def test_maps_to_the_right_error_class(self, fault: FaultType, expected: ErrorClass) -> None:
        assert fault.error_class is expected

    def test_injected_fault_is_labelled_in_its_message_and_details(self) -> None:
        """§4.4 requires the UI to show TEST FAULT. The label travels with the
        error so it cannot be lost between the worker and the attempt record."""
        error = InjectedFault(FaultType.PROVIDER_TIMEOUT, "video.clip.03")
        assert "TEST FAULT" in str(error)
        assert error.details["injected"] is True
        assert error.details["stable_key"] == "video.clip.03"
