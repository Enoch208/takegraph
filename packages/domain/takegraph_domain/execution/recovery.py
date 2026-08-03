"""Recovery policy: what to do when a provider attempt fails (PRD §5.5, §13.3).

Pure decision logic over a provider policy and the failure that occurred. No
database, no clock, no SDK — so every branch of "retry, fall back, or give up"
can be tested without burning a provider call, which is exactly the code path
you cannot afford to debug live.

The rules that shape it:

- §13.3: "Never retry invalid input, authentication failure, policy denial,
  unsupported model, or deterministic validation failure as the same attempt."
  Those are not transient; retrying them burns budget and changes nothing.
- §5.5 FR-PROV-002: same-provider model fallback and app-level cross-provider
  fallback are distinct mechanisms, and the attempt record must say which ran.
- §5.5 FR-PROV-004: attempt, elapsed-time and estimated-spend budgets are
  enforced before any new submission, and exhaustion is a typed terminal state.
- §13.3: "Quota failure may route to a configured provider fallback, but must be
  visible."
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from takegraph_domain.enums import AttemptMechanism, ErrorClass


class RecoveryAction(StrEnum):
    RETRY_SAME_MODEL = "RETRY_SAME_MODEL"
    FALLBACK_MODEL = "FALLBACK_MODEL"
    FALLBACK_PROVIDER = "FALLBACK_PROVIDER"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: RecoveryAction
    reason_code: str
    reason: str
    provider: str | None = None
    model: str | None = None
    mechanism: AttemptMechanism | None = None
    delay_seconds: int = 0
    timeout_seconds: int | None = None

    @property
    def should_retry(self) -> bool:
        return self.action is not RecoveryAction.FAIL


@dataclass(frozen=True, slots=True)
class AttemptBudget:
    """State the decision is made against. Supplied by the caller so this module
    never reads a clock or a database."""

    attempt_count: int
    transient_retries_used: int
    elapsed_seconds: float
    estimated_spend_usd: Decimal


def backoff_delay(attempt: int, policy_retry: dict[str, Any]) -> int:
    """Exponential backoff, capped (§13.3).

    Jitter is deliberately *not* applied here — this function must stay
    deterministic so its tests are meaningful. The worker adds jitter when the
    policy asks for it, which is where a random source legitimately belongs.
    """
    base = int(policy_retry.get("base_delay_seconds", 2))
    cap = int(policy_retry.get("max_delay_seconds", 30))
    return int(min(cap, base * (2 ** max(0, attempt - 1))))


def _budget_exhausted(budget: AttemptBudget, budgets: dict[str, Any]) -> str | None:
    """§5.5 FR-PROV-004. Returns the exhausted dimension, or None."""
    max_attempts = int(budgets.get("max_total_attempts", 4))
    if budget.attempt_count >= max_attempts:
        return f"attempt budget of {max_attempts} is exhausted"

    max_elapsed = budgets.get("max_elapsed_seconds")
    if max_elapsed is not None and budget.elapsed_seconds >= float(max_elapsed):
        return f"elapsed-time budget of {max_elapsed}s is exhausted"

    max_spend = budgets.get("max_estimated_cost_usd")
    if max_spend is not None and budget.estimated_spend_usd >= Decimal(str(max_spend)):
        return f"estimated-spend budget of {max_spend} USD is exhausted"

    return None


def decide_recovery(
    *,
    error_class: ErrorClass,
    policy: dict[str, Any],
    budget: AttemptBudget,
    current_model: str,
    available_credentials: frozenset[str] = frozenset(),
) -> RecoveryDecision:
    """Choose the next move after a failed attempt.

    Order matters and encodes the PRD's preference: exhaust cheap same-provider
    recovery before routing elsewhere (§UJ-05 — "Retry policy first applies
    allowed same-provider recovery. When exhausted, application policy selects a
    compatible fallback provider").
    """
    budgets = policy.get("budgets", {}) or {}
    exhausted = _budget_exhausted(budget, budgets)
    if exhausted is not None:
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            reason_code="BUDGET_EXCEEDED",
            reason=f"No further attempts: the {exhausted}.",
        )

    # §13.3: these are deterministic. A second identical submission produces the
    # same failure and costs money to discover that.
    if error_class in (
        ErrorClass.INPUT,
        ErrorClass.AUTH,
        ErrorClass.POLICY,
        ErrorClass.VALIDATION,
    ):
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            reason_code=f"NON_RETRYABLE_{error_class.value}",
            reason=(
                f"A {error_class.value.lower()} failure is deterministic; "
                "retrying the same submission would fail identically."
            ),
        )

    primary = policy.get("primary", {}) or {}
    retry_config = policy.get("retry", {}) or {}
    max_transient = int(retry_config.get("max_transient_retries", 2))

    # 1. Same model, same provider — cheapest recovery, and the right response to
    #    a genuinely transient fault.
    # The eligible set lives on ErrorClass so the rule is stated once. Inlining it
    # here is how the implementation drifted from the documented one.
    if error_class.is_retryable_same_provider and budget.transient_retries_used < max_transient:
        return RecoveryDecision(
            action=RecoveryAction.RETRY_SAME_MODEL,
            reason_code="TRANSIENT_RETRY",
            reason=(
                f"{error_class.value.title()} failure; retrying the same model "
                f"({budget.transient_retries_used + 1} of {max_transient})."
            ),
            provider=primary.get("provider"),
            model=current_model,
            mechanism=AttemptMechanism.SAME_PROVIDER_RETRY,
            delay_seconds=backoff_delay(budget.transient_retries_used + 1, retry_config),
            timeout_seconds=primary.get("timeout_seconds"),
        )

    # 2. A different model on the same provider. Covers a model-specific fault
    #    without changing vendor.
    for candidate in primary.get("same_provider_fallback_models", []) or []:
        if candidate and candidate != current_model and not _unresolved(candidate):
            return RecoveryDecision(
                action=RecoveryAction.FALLBACK_MODEL,
                reason_code="SAME_PROVIDER_MODEL_FALLBACK",
                reason=(
                    f"{error_class.value.lower()} failure on {current_model}; "
                    f"falling back to {candidate} on the same provider."
                ),
                provider=primary.get("provider"),
                model=candidate,
                mechanism=AttemptMechanism.SAME_PROVIDER_MODEL_FALLBACK,
                timeout_seconds=primary.get("timeout_seconds"),
            )

    # 3. Another provider entirely. §14.3 makes this a new parent-linked child
    #    run rather than a built-in provider feature, so it is visible as a
    #    distinct mechanism in the attempt record.
    for fallback in policy.get("cross_provider_fallbacks", []) or []:
        model = fallback.get("model")
        provider = fallback.get("provider")
        if not model or not provider or _unresolved(model):
            continue

        # §9.2: "Missing optional provider credentials remove that fallback from
        # readiness but do not pretend it ran." Skipping silently would leave the
        # build looking like it had no fallback configured at all, so the reason
        # below names the missing credential when nothing is left.
        required = fallback.get("required_credential")
        if required and required not in available_credentials:
            continue

        return RecoveryDecision(
            action=RecoveryAction.FALLBACK_PROVIDER,
            reason_code="CROSS_PROVIDER_FALLBACK",
            reason=(
                f"{error_class.value.lower()} failure on {primary.get('provider')}; "
                f"routing to {provider} as a parent-linked child run."
            ),
            provider=provider,
            model=model,
            mechanism=AttemptMechanism.CROSS_PROVIDER_FALLBACK,
            timeout_seconds=fallback.get("timeout_seconds"),
        )

    configured = [
        f.get("required_credential")
        for f in (policy.get("cross_provider_fallbacks") or [])
        if f.get("required_credential") and f["required_credential"] not in available_credentials
    ]
    if configured:
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            reason_code="FALLBACK_NOT_CONFIGURED",
            reason=(
                "Same-provider recovery is exhausted and every cross-provider fallback is "
                f"missing its credential ({', '.join(sorted(filter(None, configured)))}). "
                "The fallback did not run."
            ),
        )

    return RecoveryDecision(
        action=RecoveryAction.FAIL,
        reason_code="RECOVERY_EXHAUSTED",
        reason="Same-provider recovery is exhausted and no cross-provider fallback is configured.",
    )


def _unresolved(value: str) -> bool:
    """`${GMI_VIDEO_MODEL}` placeholders are resolved server-side when a policy
    becomes an execution plan (§9.2). One still wearing its braces here means the
    environment never supplied it, and submitting it verbatim would be a
    guaranteed provider error."""
    return value.startswith("${") and value.endswith("}")
