"""Impact analysis (PRD §12.4) and the reuse proof (§12.3).

Side-effect free by construction: this module computes what *would* happen. It
touches no database, makes no provider call, and writes nothing to B2
(FR-IMPACT-001). Identical inputs produce an identical plan and plan hash
(FR-IMPACT-005), which is what lets the commit endpoint reject a stale plan.

The cascade works through output references rather than through explicit graph
walking: a node that will rebuild advertises a `pending:` placeholder instead of
its previously selected asset hash, so every descendant that consumes it
fingerprints differently and is conservatively invalid. §12.4 requires exactly
that conservatism — a rebuilt node's future bytes are unknown, so anything
downstream of it cannot be assumed reusable.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from takegraph_domain.canonical import canonical_hash
from takegraph_domain.enums import ImpactDecision, PricingStatus, ReasonCode
from takegraph_domain.graph.fingerprint import (
    compute_fingerprint,
    compute_source_fingerprint,
    pending_output_ref,
)
from takegraph_domain.graph.types import SCHEMA_VERSION, CompiledGraph, NodeCacheState


class ReuseRejection(BaseModel):
    """Why a cache candidate failed the reuse proof. Never a bare bool — §5.3
    FR-IMPACT-002 requires a machine code and a human-readable reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason_code: ReasonCode
    reason: str


def evaluate_reuse_proof(
    *,
    proposed_fingerprint: str,
    candidate: NodeCacheState | None,
    allow_fixture_reuse: bool = False,
) -> ReuseRejection | None:
    """§12.3. Returns None when reuse is proven, otherwise why it was refused.

    Ordering matters for explanation quality: the cheapest and most informative
    check runs first, so a user sees "the recipe changed" rather than an incidental
    downstream complaint about validation staleness.
    """
    if candidate is None:
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_MISS,
            reason="No previous build produced this node.",
        )

    if candidate.fingerprint != proposed_fingerprint:
        return ReuseRejection(
            reason_code=ReasonCode.NODE_SPEC_CHANGED,
            reason="The node's recipe or its selected inputs changed.",
        )

    if candidate.revoked:
        return ReuseRejection(
            reason_code=ReasonCode.MANUAL_INVALIDATION,
            reason="This output was manually invalidated.",
        )

    accepted = candidate.status.satisfies_dependency or candidate.manually_approved
    if not accepted:
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_MISS,
            reason=f"The previous attempt ended {candidate.status.value}, not an accepted state.",
        )

    if candidate.selected_output_hash is None:
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_ASSET_MISSING,
            reason="The previous build node has no selected output.",
        )

    if not candidate.assets_present:
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_ASSET_MISSING,
            reason="A selected asset is no longer present in B2.",
        )

    if not candidate.assets_verified:
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_ASSET_UNVERIFIED,
            reason="Stored bytes no longer match the recorded SHA-256.",
        )

    if not candidate.validations_current:
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_VALIDATION_STALE,
            reason="Required quality gates are not current for this validation policy.",
        )

    if candidate.is_fixture and not allow_fixture_reuse:
        # §12.3: a fixture candidate is reusable only when the current build is
        # itself fixture/replay scoped. This is the check that stops seeded demo
        # data leaking onto a path the UI presents as live (§0.1).
        return ReuseRejection(
            reason_code=ReasonCode.CACHE_MISS,
            reason="Candidate came from a fixture and this build is not fixture-scoped.",
        )

    return None


class NodeImpact(BaseModel):
    """§9.5 per-node plan entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_key: str
    decision: ImpactDecision
    reason_code: ReasonCode
    reason: str
    old_fingerprint: str | None
    new_fingerprint: str
    provider_calls: int
    estimated_cost_usd: str | None
    requires_human_review: bool


class ImpactSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reuse: int
    rebuild: int
    review: int
    blocked: int
    provider_calls: int
    estimated_cost_usd: str | None
    pricing_status: PricingStatus


class ImpactPlan(BaseModel):
    """§9.5. Immutable preview evidence, bound to a graph revision by `plan_hash`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    graph_canonical_hash: str
    nodes: tuple[NodeImpact, ...]
    summary: ImpactSummary
    plan_hash: str

    @property
    def rebuild_keys(self) -> tuple[str, ...]:
        return tuple(n.stable_key for n in self.nodes if n.decision is ImpactDecision.REBUILD)

    @property
    def reuse_keys(self) -> tuple[str, ...]:
        return tuple(n.stable_key for n in self.nodes if n.decision is ImpactDecision.REUSE)


def compute_impact(
    graph: CompiledGraph,
    *,
    base_states: Mapping[str, NodeCacheState],
    source_content_hashes: Mapping[str, str],
    generator_code_version: str,
    blocked_keys: Mapping[str, str] | None = None,
    allow_fixture_reuse: bool = False,
) -> ImpactPlan:
    """Walk the graph in topological order and decide each node (§12.4).

    `blocked_keys` maps a stable key to the reason it cannot execute — a missing
    credential or unconfigured capability. Those become BLOCKED rather than
    REBUILD, because promising a rebuild the system cannot perform would be a lie
    the user only discovers after committing.
    """
    blocked_keys = blocked_keys or {}
    by_key = graph.by_key
    template_version = graph.template_version_label

    # Each node's output reference as this plan would leave it: a real content
    # hash if reused, a pending placeholder if it will rebuild.
    output_refs: dict[str, str | None] = {}
    impacts: list[NodeImpact] = []

    for stable_key in graph.topological_order:
        node = by_key[stable_key]
        candidate = base_states.get(stable_key)

        if node.node_type.is_source:
            content_hash = source_content_hashes.get(stable_key)
            if content_hash is None:
                raise ValueError(
                    f"source node {stable_key!r} has no content hash; a source must be "
                    "uploaded and verified before impact can be computed"
                )
            proposed = compute_source_fingerprint(node, content_hash=content_hash)
        else:
            proposed = compute_fingerprint(
                node,
                input_refs=output_refs,
                generator_code_version=generator_code_version,
                template_version=template_version,
            )

        old_fingerprint = candidate.fingerprint if candidate else None

        rejection = evaluate_reuse_proof(
            proposed_fingerprint=proposed,
            candidate=candidate,
            allow_fixture_reuse=allow_fixture_reuse,
        )

        if rejection is None:
            if candidate is not None and candidate.selected_output_hash is not None:
                output_refs[stable_key] = candidate.selected_output_hash
                impacts.append(
                    NodeImpact(
                        stable_key=stable_key,
                        decision=ImpactDecision.REUSE,
                        reason_code=ReasonCode.EXACT_VALIDATED_REUSE,
                        reason="Recipe, inputs, stored bytes and quality gates are all unchanged.",
                        old_fingerprint=old_fingerprint,
                        new_fingerprint=proposed,
                        provider_calls=0,
                        estimated_cost_usd=None,
                        requires_human_review=False,
                    )
                )
                continue

            # Unreachable while evaluate_reuse_proof checks both conditions, but the
            # type system cannot prove that across the call. Degrading to a cache miss
            # keeps the two in sync if the proof is ever relaxed: the cost is a
            # needless rebuild, whereas trusting a nonexistent output would select an
            # asset that is not there.
            rejection = ReuseRejection(
                reason_code=ReasonCode.CACHE_ASSET_MISSING,
                reason="Reuse was proven but the candidate has no selected output.",
            )

        # A provider credential is needed only when this plan must execute the
        # node. Exact validated reuse remains valid after key rotation or when a
        # no-longer-needed provider is temporarily unconfigured.
        if stable_key in blocked_keys:
            impacts.append(
                NodeImpact(
                    stable_key=stable_key,
                    decision=ImpactDecision.BLOCKED,
                    reason_code=ReasonCode.CONFIGURATION_BLOCKED,
                    reason=blocked_keys[stable_key],
                    old_fingerprint=old_fingerprint,
                    new_fingerprint=proposed,
                    provider_calls=0,
                    estimated_cost_usd=None,
                    requires_human_review=True,
                )
            )
            output_refs[stable_key] = None
            continue

        reason_code, reason = _refine_rebuild_reason(
            node_key=stable_key,
            rejection_code=rejection.reason_code,
            rejection_reason=rejection.reason,
            graph=graph,
            output_refs=output_refs,
            base_states=base_states,
        )

        output_refs[stable_key] = pending_output_ref(proposed)
        impacts.append(
            NodeImpact(
                stable_key=stable_key,
                decision=ImpactDecision.REBUILD,
                reason_code=reason_code,
                reason=reason,
                old_fingerprint=old_fingerprint,
                new_fingerprint=proposed,
                provider_calls=1 if node.node_type.calls_provider else 0,
                # §5.3 FR-IMPACT-003: unknown pricing stays unknown, never zero.
                estimated_cost_usd=None,
                requires_human_review=False,
            )
        )

    summary = _summarize(impacts)
    plan = ImpactPlan(
        graph_canonical_hash=graph.canonical_hash,
        nodes=tuple(impacts),
        summary=summary,
        plan_hash="",
    )
    return plan.model_copy(update={"plan_hash": _plan_hash(plan)})


def _refine_rebuild_reason(
    *,
    node_key: str,
    rejection_code: ReasonCode,
    rejection_reason: str,
    graph: CompiledGraph,
    output_refs: Mapping[str, str | None],
    base_states: Mapping[str, NodeCacheState],
) -> tuple[ReasonCode, str]:
    """Distinguish "this node's own recipe changed" from "something upstream did".

    Both surface as a fingerprint mismatch, but they are different stories to a
    user, and §5.3 FR-IMPACT-002 requires the reason to be deterministic and
    explanatory. If any upstream node is rebuilding in this same plan, the cause is
    upstream; otherwise the node's own spec or source content moved.
    """
    if rejection_code is not ReasonCode.NODE_SPEC_CHANGED:
        return rejection_code, rejection_reason

    node = graph.by_key[node_key]
    changed_upstream = sorted(
        {
            slot.from_key
            for slot in node.inputs
            if (ref := output_refs.get(slot.from_key)) is not None and ref.startswith("pending:")
        }
    )
    if changed_upstream:
        return (
            ReasonCode.UPSTREAM_FINGERPRINT_CHANGED,
            f"{', '.join(changed_upstream)} will be rebuilt, so this node's input changes.",
        )

    if node.node_type.is_source:
        return ReasonCode.SOURCE_CONTENT_CHANGED, "The source content changed."

    if node_key not in base_states:
        return ReasonCode.NODE_ADDED, "This node is new in the proposed graph."

    return ReasonCode.NODE_SPEC_CHANGED, "This node's own specification changed."


def _summarize(impacts: list[NodeImpact]) -> ImpactSummary:
    counts = {decision: 0 for decision in ImpactDecision}
    for impact in impacts:
        counts[impact.decision] += 1
    return ImpactSummary(
        reuse=counts[ImpactDecision.REUSE],
        rebuild=counts[ImpactDecision.REBUILD],
        review=counts[ImpactDecision.REVIEW],
        blocked=counts[ImpactDecision.BLOCKED],
        provider_calls=sum(i.provider_calls for i in impacts),
        # Pricing is unresolved at plan time and stays that way until a registry
        # entry exists for every model involved. Reporting 0 would be a fabrication.
        estimated_cost_usd=None,
        pricing_status=PricingStatus.UNKNOWN,
    )


def _plan_hash(plan: ImpactPlan) -> str:
    """§11.5 binds commit to this hash; §5.3 FR-IMPACT-005 requires identical inputs
    to produce it identically. Node order follows the graph's topological order,
    which is itself deterministic."""
    return canonical_hash(
        {
            "schema_version": plan.schema_version,
            "graph_canonical_hash": plan.graph_canonical_hash,
            "nodes": [
                {
                    "stable_key": n.stable_key,
                    "decision": str(n.decision),
                    "reason_code": str(n.reason_code),
                    "new_fingerprint": n.new_fingerprint,
                    "old_fingerprint": n.old_fingerprint,
                    "provider_calls": n.provider_calls,
                }
                for n in plan.nodes
            ],
        }
    )
