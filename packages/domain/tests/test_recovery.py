"""Recovery policy tests — PRD §5.5, §13.3, UJ-05, AS-02.

The two failure modes are opposite and both expensive:

- Retrying something deterministic (bad input, bad credentials) burns the attempt
  budget discovering the same answer, then fails anyway.
- Giving up on something transient turns a recoverable blip into a failed build.

Every test below pins one side. Costs are strings and Decimals throughout —
§8.1 forbids binary floats for money, and a budget check is exactly where a
rounding error would silently authorise one attempt too many.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from takegraph_domain.enums import AttemptMechanism, ErrorClass
from takegraph_domain.execution.recovery import (
    AttemptBudget,
    RecoveryAction,
    backoff_delay,
    decide_recovery,
)

POLICY = {
    "primary": {
        "provider": "gmicloud",
        "model": "wan2.6-r2v",
        "timeout_seconds": 480,
        "same_provider_fallback_models": ["pixverse-v5.6-i2v"],
    },
    "cross_provider_fallbacks": [
        {
            "provider": "runway",
            "model": "gen-3",
            "timeout_seconds": 480,
            "required_credential": "RUNWAYML_API_SECRET",
        }
    ],
    "retry": {"max_transient_retries": 2, "base_delay_seconds": 2, "max_delay_seconds": 30},
    "budgets": {
        "max_total_attempts": 4,
        "max_elapsed_seconds": 900,
        "max_estimated_cost_usd": "5.000000",
    },
}

FRESH = AttemptBudget(
    attempt_count=1,
    transient_retries_used=0,
    elapsed_seconds=12.0,
    estimated_spend_usd=Decimal("0.10"),
)

WITH_CREDS = frozenset({"RUNWAYML_API_SECRET"})


def decide(**kw):
    params = {
        "error_class": ErrorClass.TRANSIENT,
        "policy": POLICY,
        "budget": FRESH,
        "current_model": "wan2.6-r2v",
        "available_credentials": WITH_CREDS,
    }
    return decide_recovery(**{**params, **kw})


class TestNonRetryable:
    """§13.3: never retry invalid input, auth failure, policy denial or a
    deterministic validation failure as the same attempt."""

    @pytest.mark.parametrize(
        "error_class",
        [ErrorClass.INPUT, ErrorClass.AUTH, ErrorClass.POLICY, ErrorClass.VALIDATION],
    )
    def test_deterministic_failures_do_not_retry(self, error_class: ErrorClass) -> None:
        decision = decide(error_class=error_class)
        assert decision.action is RecoveryAction.FAIL
        assert decision.should_retry is False
        assert error_class.value in decision.reason_code

    def test_auth_failure_does_not_silently_route_to_another_provider(self) -> None:
        """Bad credentials on the primary say nothing about the fallback, but
        burning a second vendor's quota to discover that is not recovery."""
        assert decide(error_class=ErrorClass.AUTH).action is RecoveryAction.FAIL


class TestTransientRetry:
    def test_transient_retries_the_same_model_first(self) -> None:
        """UJ-05: "Retry policy first applies allowed same-provider recovery"."""
        decision = decide(error_class=ErrorClass.TRANSIENT)
        assert decision.action is RecoveryAction.RETRY_SAME_MODEL
        assert decision.model == "wan2.6-r2v"
        assert decision.mechanism is AttemptMechanism.SAME_PROVIDER_RETRY

    def test_storage_failure_retries_before_regenerating(self) -> None:
        """§13.3: "Storage verification failure retries storage/fetch before
        regenerating media." Regenerating is far more expensive."""
        assert decide(error_class=ErrorClass.STORAGE).action is RecoveryAction.RETRY_SAME_MODEL

    def test_retry_uses_exponential_backoff(self) -> None:
        first = decide(budget=AttemptBudget(1, 0, 10.0, Decimal("0.1")))
        second = decide(budget=AttemptBudget(2, 1, 20.0, Decimal("0.2")))
        assert second.delay_seconds > first.delay_seconds

    def test_exhausting_transient_retries_moves_to_model_fallback(self) -> None:
        decision = decide(budget=AttemptBudget(3, 2, 30.0, Decimal("0.3")))
        assert decision.action is RecoveryAction.FALLBACK_MODEL
        assert decision.model == "pixverse-v5.6-i2v"


class TestFallbackOrdering:
    """§5.5 FR-PROV-002 keeps the two fallback mechanisms distinct, and the order
    is cheapest-first."""

    def test_model_fallback_precedes_provider_fallback(self) -> None:
        decision = decide(
            error_class=ErrorClass.MODEL, budget=AttemptBudget(2, 2, 30.0, Decimal("0.2"))
        )
        assert decision.action is RecoveryAction.FALLBACK_MODEL
        assert decision.provider == "gmicloud"

    def test_provider_fallback_when_no_model_alternative_remains(self) -> None:
        """AS-02: a cross-provider child attempt with the routing reason stored."""
        policy = {**POLICY, "primary": {**POLICY["primary"], "same_provider_fallback_models": []}}
        decision = decide(
            error_class=ErrorClass.TRANSIENT,
            policy=policy,
            budget=AttemptBudget(3, 2, 40.0, Decimal("0.4")),
        )
        assert decision.action is RecoveryAction.FALLBACK_PROVIDER
        assert decision.provider == "runway"
        assert decision.mechanism is AttemptMechanism.CROSS_PROVIDER_FALLBACK
        assert "runway" in decision.reason

    def test_model_fallback_skips_the_model_that_just_failed(self) -> None:
        """Falling back to the same model would be a retry wearing a different
        label, and would misreport the mechanism on the attempt record."""
        policy = {
            **POLICY,
            "primary": {**POLICY["primary"], "same_provider_fallback_models": ["wan2.6-r2v"]},
        }
        decision = decide(policy=policy, budget=AttemptBudget(3, 2, 30.0, Decimal("0.3")))
        assert decision.action is not RecoveryAction.FALLBACK_MODEL

    def test_quota_failure_routes_to_a_fallback_provider(self) -> None:
        (
            """§13.3: "Quota failure may route to a configured provider fallback, but
        must be visible."""
            ""
        )
        policy = {**POLICY, "primary": {**POLICY["primary"], "same_provider_fallback_models": []}}
        decision = decide(
            error_class=ErrorClass.QUOTA,
            policy=policy,
            budget=AttemptBudget(2, 2, 20.0, Decimal("0.2")),
        )
        assert decision.action is RecoveryAction.FALLBACK_PROVIDER
        assert decision.reason_code == "CROSS_PROVIDER_FALLBACK"


class TestUnconfiguredFallback:
    """§9.2: "Missing optional provider credentials remove that fallback from
    readiness but do not pretend it ran."""

    def test_missing_credential_disqualifies_the_fallback(self) -> None:
        policy = {**POLICY, "primary": {**POLICY["primary"], "same_provider_fallback_models": []}}
        decision = decide(
            policy=policy,
            budget=AttemptBudget(3, 2, 40.0, Decimal("0.4")),
            available_credentials=frozenset(),
        )
        assert decision.action is RecoveryAction.FAIL
        assert decision.reason_code == "FALLBACK_NOT_CONFIGURED"

    def test_the_reason_names_the_missing_credential(self) -> None:
        """A build that stops because a key is absent must say which key, or the
        operator is left guessing why recovery did not happen."""
        policy = {**POLICY, "primary": {**POLICY["primary"], "same_provider_fallback_models": []}}
        decision = decide(
            policy=policy,
            budget=AttemptBudget(3, 2, 40.0, Decimal("0.4")),
            available_credentials=frozenset(),
        )
        assert "RUNWAYML_API_SECRET" in decision.reason
        assert "did not run" in decision.reason

    def test_unresolved_placeholder_is_not_submitted(self) -> None:
        """§9.2 resolves ${VAR} server-side. One still wearing its braces means
        the environment never supplied it, and sending it verbatim guarantees a
        provider error."""
        policy = {
            **POLICY,
            "primary": {**POLICY["primary"], "same_provider_fallback_models": ["${GMI_FALLBACK}"]},
        }
        decision = decide(policy=policy, budget=AttemptBudget(3, 2, 30.0, Decimal("0.3")))
        assert decision.action is not RecoveryAction.FALLBACK_MODEL


class TestBudgets:
    """§5.5 FR-PROV-004: attempt, elapsed and spend budgets produce a typed
    terminal state rather than an unbounded loop."""

    def test_attempt_budget_stops_recovery(self) -> None:
        decision = decide(budget=AttemptBudget(4, 0, 10.0, Decimal("0.1")))
        assert decision.action is RecoveryAction.FAIL
        assert decision.reason_code == "BUDGET_EXCEEDED"
        assert "attempt budget" in decision.reason

    def test_elapsed_budget_stops_recovery(self) -> None:
        decision = decide(budget=AttemptBudget(2, 0, 900.0, Decimal("0.1")))
        assert decision.reason_code == "BUDGET_EXCEEDED"
        assert "elapsed-time" in decision.reason

    def test_spend_budget_stops_recovery(self) -> None:
        decision = decide(budget=AttemptBudget(2, 0, 10.0, Decimal("5.000000")))
        assert decision.reason_code == "BUDGET_EXCEEDED"
        assert "spend" in decision.reason

    def test_spend_check_uses_decimal_not_float(self) -> None:
        """A cent under the cap must still be allowed. Float arithmetic here would
        make the boundary unpredictable."""
        decision = decide(budget=AttemptBudget(2, 0, 10.0, Decimal("4.999999")))
        assert decision.action is not RecoveryAction.FAIL

    def test_budget_outranks_a_retryable_error(self) -> None:
        """Exhaustion is checked first: a transient error with no budget left is
        still terminal."""
        decision = decide(
            error_class=ErrorClass.TRANSIENT, budget=AttemptBudget(9, 0, 10.0, Decimal("0.1"))
        )
        assert decision.action is RecoveryAction.FAIL


class TestBackoff:
    def test_grows_exponentially(self) -> None:
        config = {"base_delay_seconds": 2, "max_delay_seconds": 30}
        assert [backoff_delay(n, config) for n in (1, 2, 3)] == [2, 4, 8]

    def test_is_capped(self) -> None:
        config = {"base_delay_seconds": 2, "max_delay_seconds": 30}
        assert backoff_delay(10, config) == 30

    def test_is_deterministic(self) -> None:
        """Jitter belongs in the worker, where a random source is legitimate.
        Keeping it out here is what makes these tests meaningful."""
        config = {"base_delay_seconds": 2, "max_delay_seconds": 30}
        assert backoff_delay(3, config) == backoff_delay(3, config)
