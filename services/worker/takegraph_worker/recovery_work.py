"""Worker-side recovery: turn a failed attempt into a next attempt (PRD §5.5, UJ-05).

The decision itself lives in `takegraph_domain.execution.recovery` and is pure.
This module supplies it with real state — attempt history, elapsed time, spend,
which credentials exist — and applies whatever it decides.

Applying a decision means three things happen together, in one transaction
(§8.5): the node moves to the matching pending state, a fresh work item is
enqueued, and a domain event records the routing reason. A partial application
would leave a node that is neither failed nor scheduled, which is the state a
build hangs in forever.
"""

from __future__ import annotations

import os
import random
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_api.db.models import Attempt, Build, BuildNode, DomainEvent, Project, ProviderPolicy
from takegraph_api.queue import WorkQueue
from takegraph_domain.builds.state_machine import assert_transition
from takegraph_domain.canonical import JsonValue
from takegraph_domain.enums import AttemptMechanism, BuildNodeStatus, ErrorClass
from takegraph_domain.execution.idempotency import work_item_dedupe_key
from takegraph_domain.execution.recovery import (
    AttemptBudget,
    RecoveryAction,
    RecoveryDecision,
    decide_recovery,
)

#: Which node state each recovery action parks in before requeueing. §10.2 gives
#: these distinct states precisely so the UI can say *why* a node is waiting.
_PENDING_STATE = {
    RecoveryAction.RETRY_SAME_MODEL: BuildNodeStatus.RETRY_PENDING,
    RecoveryAction.FALLBACK_MODEL: BuildNodeStatus.FALLBACK_PENDING,
    RecoveryAction.FALLBACK_PROVIDER: BuildNodeStatus.FALLBACK_PENDING,
}

#: Environment variables that gate a cross-provider fallback. A fallback whose
#: credential is absent is skipped with its name reported (§9.2), never silently.
_CREDENTIAL_VARS = (
    "RUNWAYML_API_SECRET",
    "GMI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ANTHROPIC_API_KEY",
)


def available_credentials() -> frozenset[str]:
    return frozenset(name for name in _CREDENTIAL_VARS if os.environ.get(name))


async def _budget(session: AsyncSession, node: BuildNode) -> AttemptBudget:
    rows = (
        (
            await session.execute(
                select(Attempt).where(Attempt.build_node_id == node.id).order_by(Attempt.attempt_no)
            )
        )
        .scalars()
        .all()
    )
    transient = sum(1 for a in rows if a.mechanism == str(AttemptMechanism.SAME_PROVIDER_RETRY))
    spend = sum((a.estimated_cost_usd or Decimal("0")) for a in rows) or Decimal("0")

    started = node.started_at or (rows[0].created_at if rows else datetime.now(UTC))
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - started).total_seconds()

    return AttemptBudget(
        attempt_count=len(rows),
        transient_retries_used=transient,
        elapsed_seconds=max(0.0, elapsed),
        estimated_spend_usd=Decimal(spend),
    )


_PLACEHOLDER = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def resolve_placeholders(value: object, env: Mapping[str, str]) -> object:
    """Expand ${VAR} against the environment, recursively.

    §9.2: "Environment placeholders are resolved server-side when a policy
    becomes an execution plan." A policy is stored with its placeholders intact
    so the same row works across environments, which means anything reading it
    for execution has to resolve them. Leaving a fallback as a literal
    "${GMI_VIDEO_FALLBACK_MODEL}" makes it look unconfigured, and recovery
    silently skips a fallback that was in fact available.

    A variable with no value stays as its placeholder, so the caller can tell the
    difference between "not configured" and "configured as empty".
    """
    if isinstance(value, str):
        match = _PLACEHOLDER.match(value)
        if match is None:
            return value
        return env.get(match.group(1)) or value
    if isinstance(value, list):
        return [resolve_placeholders(item, env) for item in value]
    if isinstance(value, dict):
        return {key: resolve_placeholders(item, env) for key, item in value.items()}
    return value


async def _policy_definition(session: AsyncSession, node: BuildNode) -> dict[str, JsonValue]:
    """The node's provider policy with placeholders resolved, or an empty policy.

    An empty policy is not a silent default: `decide_recovery` reads it as no
    fallbacks and default budgets, so the node fails after its retries rather
    than routing somewhere unconfigured.
    """
    from takegraph_api.db.models import GraphNode

    graph_node = await session.get(GraphNode, node.graph_node_id)
    if graph_node is None or graph_node.provider_policy_id is None:
        return {}
    policy = await session.get(ProviderPolicy, graph_node.provider_policy_id)
    if policy is None:
        return {}
    resolved = resolve_placeholders(dict(policy.definition_json), os.environ)
    return cast("dict[str, JsonValue]", resolved)


async def plan_recovery(
    session: AsyncSession,
    *,
    node: BuildNode,
    error_class: str,
    current_model: str,
) -> RecoveryDecision:
    """Decide the next move for a node whose attempt just failed."""
    try:
        parsed = ErrorClass(error_class)
    except ValueError:
        # An unmapped provider error is INTERNAL, which is retryable-adjacent but
        # not transient — it will fall through to fallback selection rather than
        # spinning on the same model.
        parsed = ErrorClass.INTERNAL

    return decide_recovery(
        error_class=parsed,
        policy=await _policy_definition(session, node),
        budget=await _budget(session, node),
        current_model=current_model,
        available_credentials=available_credentials(),
    )


async def apply_recovery(
    session: AsyncSession,
    *,
    build: Build,
    project: Project,
    node: BuildNode,
    failed_attempt: Attempt,
    decision: RecoveryDecision,
) -> bool:
    """Park the node, enqueue the next attempt, record why. Returns False when the
    decision is to fail, leaving the caller to terminate the node.

    The next attempt's provider and model ride on the work item payload, so the
    handler that picks it up submits to the decided target rather than
    recomputing the policy and possibly reaching a different answer.
    """
    if decision.action is RecoveryAction.FAIL:
        return False

    pending_state = _PENDING_STATE[decision.action]
    assert_transition(BuildNodeStatus(node.status), pending_state, subject="node")
    node.status = str(pending_state)
    node.reason_code = decision.reason_code
    node.reason = decision.reason
    node.version += 1

    delay = decision.delay_seconds
    if delay and _jitter_enabled(await _policy_definition(session, node)):
        # §13.3 asks for full jitter. It belongs here rather than in the domain
        # module, whose determinism is what makes its tests meaningful.
        delay = random.randint(0, delay)  # noqa: S311 — backoff spread, not a secret

    await WorkQueue(session).enqueue(
        kind="EXECUTE_BUILD_NODE",
        target_id=node.id,
        build_id=build.id,
        delay_seconds=delay,
        dedupe_key=work_item_dedupe_key(
            kind="EXECUTE_BUILD_NODE",
            target_id=node.id,
            # The parent attempt id makes each recovery a distinct queue item.
            # Without it the dedupe key would collide with the attempt that just
            # failed and the retry would silently never be scheduled.
            discriminator=f"recovery:{failed_attempt.id}",
        ),
        payload={
            "stable_key": node.stable_key,
            "trigger_source": "APPLICATION_COMMIT",
            "recovery": {
                "parent_attempt_id": str(failed_attempt.id),
                "mechanism": str(decision.mechanism) if decision.mechanism else None,
                "provider": decision.provider,
                "model": decision.model,
                "reason_code": decision.reason_code,
            },
        },
    )

    session.add(
        DomainEvent(
            event_id=uuid.uuid4(),
            organization_id=project.organization_id,
            project_id=project.id,
            build_id=build.id,
            event_type="build.node.recovery_scheduled",
            payload_json={
                "build_node_id": str(node.id),
                "stable_key": node.stable_key,
                "from": str(BuildNodeStatus.RUNNING),
                "to": str(pending_state),
                "reason_code": decision.reason_code,
                "reason": decision.reason,
                "mechanism": str(decision.mechanism) if decision.mechanism else None,
                "provider": decision.provider,
                "model": decision.model,
                "parent_attempt_id": str(failed_attempt.id),
                "delay_seconds": delay,
            },
            correlation_id=uuid.uuid4(),
        )
    )
    return True


def _jitter_enabled(policy: dict[str, JsonValue]) -> bool:
    retry = policy.get("retry")
    return isinstance(retry, dict) and bool(retry.get("jitter"))


async def next_attempt_no(session: AsyncSession, node_id: uuid.UUID) -> int:
    return int(
        (
            await session.scalar(
                select(func.max(Attempt.attempt_no)).where(Attempt.build_node_id == node_id)
            )
            or 0
        )
        + 1
    )
