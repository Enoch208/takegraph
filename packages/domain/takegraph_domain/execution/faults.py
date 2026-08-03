"""Controlled failure injection (PRD §8.3.11, §4.3, §4.4).

Exists so provider recovery can be demonstrated on demand instead of waiting for
a real outage. That makes it dangerous, so the guards are structural rather than
conventional:

- §8.3.11: a rule is rejected unless `ALLOW_FAILURE_INJECTION=true` *and* the
  project is explicitly demo/test scoped. Both, not either.
- §4.4: an injected failure is labelled `TEST FAULT` in the UI. The label comes
  from `attempts.is_injected_fault`, so it is stored evidence rather than a
  guess the frontend makes.
- Rules are consumed and expire. A rule that fired once and stayed armed would
  turn a demo into a permanently broken build.

§0.1 forbids fabricating provider failures. An injected fault is not a fabricated
one: nothing is recorded as having come from a provider, the attempt is marked as
injected, and the recovery it triggers is genuine — a real fallback submission to
a real second provider.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from takegraph_domain.enums import ErrorClass
from takegraph_domain.errors import DomainError, ForbiddenError


class FaultType(StrEnum):
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    STORAGE_FAILURE = "STORAGE_FAILURE"

    @property
    def error_class(self) -> ErrorClass:
        """How the recovery policy should read this fault. A timeout is transient
        and retryable; a quota failure routes to a fallback provider (§13.3)."""
        return {
            FaultType.PROVIDER_TIMEOUT: ErrorClass.TRANSIENT,
            FaultType.PROVIDER_ERROR: ErrorClass.MODEL,
            FaultType.PROVIDER_QUOTA: ErrorClass.QUOTA,
            FaultType.STORAGE_FAILURE: ErrorClass.STORAGE,
        }[self]


class FaultInjectionForbiddenError(ForbiddenError):
    """Raised when a rule is requested outside the conditions §8.3.11 permits."""


class InjectedFault(DomainError):
    """The failure a matched rule raises.

    A distinct type so the worker cannot confuse it with a real provider error
    and, more importantly, so it can be labelled honestly on the attempt.
    """

    def __init__(self, fault_type: FaultType, stable_key: str) -> None:
        super().__init__(
            f"TEST FAULT: injected {fault_type.value} on {stable_key}.",
            details={"fault_type": str(fault_type), "stable_key": stable_key, "injected": True},
        )
        self.fault_type = fault_type


@dataclass(frozen=True, slots=True)
class FaultRule:
    id: uuid.UUID
    project_id: uuid.UUID
    node_stable_key: str
    fault_type: FaultType
    remaining_uses: int
    expires_at: datetime | None

    def is_armed(self, *, now: datetime) -> bool:
        if self.remaining_uses <= 0:
            return False
        return not (self.expires_at is not None and self.expires_at <= now)


def assert_injection_allowed(*, allow_failure_injection: bool, project_is_demo: bool) -> None:
    """§8.3.11, both conditions required.

    Checked here rather than at the route so the worker cannot fire a rule that
    was armed while the flag was on and the flag has since been turned off.
    """
    if not allow_failure_injection:
        raise FaultInjectionForbiddenError(
            "Failure injection is disabled. Set ALLOW_FAILURE_INJECTION=true to enable it "
            "in a demo or test environment."
        )
    if not project_is_demo:
        raise FaultInjectionForbiddenError(
            "Failure injection is only permitted on a project explicitly scoped as demo or test."
        )


def select_fault(
    rules: list[FaultRule],
    *,
    stable_key: str,
    now: datetime,
    allow_failure_injection: bool,
    project_is_demo: bool,
) -> FaultRule | None:
    """The armed rule for this node, or None.

    Returns None rather than raising when injection is disabled: a stale rule in
    the database must not break an ordinary build. Only an explicit *request* to
    create one is refused loudly.
    """
    if not allow_failure_injection or not project_is_demo:
        return None
    for rule in rules:
        if rule.node_stable_key == stable_key and rule.is_armed(now=now):
            return rule
    return None
