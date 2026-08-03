"""Seed projection for the landing page's proof strip.

PRD §4.4 is explicit: "Seed metrics must be loaded from raw events, never
hard-coded in React." This module is how the landing page gets real numbers today
— it runs the actual impact engine over the seed template rather than shipping
"14" and "4" as strings in a component.

It is a *projection*, not build evidence. No build has run, nothing is in B2, and
the response says so in `source` and `verified_build` so the UI can label it
truthfully (§0.1 forbids unlabeled demo data on a path that appears live). Once a
real baseline build exists, this module is replaced by a query over persisted
`domain_events` and the two fields change to say so — the shape does not.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.enums import BuildNodeStatus, PricingStatus
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.fingerprint import compute_fingerprint, compute_source_fingerprint
from takegraph_domain.graph.impact import compute_impact
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    DEFAULT_LEGAL_LINE,
    ORBIT_TEMPLATE,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
    POSTER_KEY,
    REFERENCED_POLICIES,
)
from takegraph_domain.graph.types import CompiledGraph, NodeCacheState

from takegraph_api.db.models import (
    Asset,
    AttemptAsset,
    Build,
    BuildNode,
    DomainEvent,
    Project,
)

GENERATOR_CODE_VERSION = "seed-projection-1"

ORIGINAL_LEGAL_LINE = DEFAULT_LEGAL_LINE
REVISED_LEGAL_LINE = "no added sugar"
BRIEF_TEXT = DEFAULT_BRIEF_TEXT

_SOURCE_CONTENT_HASHES = {
    "source.brief": hashlib.sha256(BRIEF_TEXT.encode()).hexdigest(),
    "source.product_reference": hashlib.sha256(b"orbit-product-reference").hexdigest(),
}

_POLICY_HASHES = {key: hashlib.sha256(key.encode()).hexdigest() for key in REFERENCED_POLICIES}


class NodeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_key: str
    label: str
    decision: str
    reason_code: str
    reason: str
    provider_calls: int


class DemoProof(BaseModel):
    """Response for GET /api/v1/demo/proof."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"

    source: Literal["TEMPLATE_PROJECTION", "BUILD_EVENTS"]
    """Where these numbers came from. TEMPLATE_PROJECTION means the impact engine
    computed them from the seed template; BUILD_EVENTS means they were derived
    from a real build's persisted events. The UI must label the difference."""

    verified_build: bool
    """True only when a real build produced and stored the referenced assets."""

    template: str
    total_nodes: int
    reuse: int
    rebuild: int
    review: int
    blocked: int
    provider_calls: int
    pricing_status: str
    estimated_cost_usd: str | None
    change_from: str
    change_to: str
    rebuild_nodes: list[NodeDecision]
    plan_hash: str
    graph_hash: str

    poster_url: str | None = None
    """Short-lived signed URL for a real poster frame from the verified build.
    None when no build has produced one — the page shows no preview rather than
    a placeholder standing in for real output (§0.1)."""


def _graph(legal_line: str) -> CompiledGraph:
    return compile_graph(
        ORBIT_TEMPLATE,
        parameters={PARAM_LEGAL_LINE: legal_line, PARAM_BRIEF_TEXT: BRIEF_TEXT},
        policy_hashes=_POLICY_HASHES,
    )


def _baseline_states(graph: CompiledGraph) -> dict[str, NodeCacheState]:
    """Project what a completed baseline build would have recorded.

    Walks the graph exactly as the worker will, so the fingerprints are the real
    ones. Generated outputs get a deterministic stand-in hash because no bytes
    exist yet — which is precisely why `verified_build` is False.
    """
    output_refs: dict[str, str | None] = {}
    states: dict[str, NodeCacheState] = {}

    for stable_key in graph.topological_order:
        node = graph.by_key[stable_key]
        if node.node_type.is_source:
            fingerprint = compute_source_fingerprint(
                node, content_hash=_SOURCE_CONTENT_HASHES[stable_key]
            )
            selected = _SOURCE_CONTENT_HASHES[stable_key]
        else:
            fingerprint = compute_fingerprint(
                node,
                input_refs=output_refs,
                generator_code_version=GENERATOR_CODE_VERSION,
                template_version=graph.template_version_label,
            )
            selected = hashlib.sha256(f"projected:{fingerprint}".encode()).hexdigest()

        output_refs[stable_key] = selected
        states[stable_key] = NodeCacheState(
            stable_key=stable_key,
            fingerprint=fingerprint,
            status=BuildNodeStatus.PASSED,
            selected_output_hash=selected,
        )
    return states


def build_demo_proof() -> DemoProof:
    """Run the real impact engine for the legal-copy change and report the result."""
    baseline = _graph(ORIGINAL_LEGAL_LINE)
    revised = _graph(REVISED_LEGAL_LINE)

    plan = compute_impact(
        revised,
        base_states=_baseline_states(baseline),
        source_content_hashes=_SOURCE_CONTENT_HASHES,
        generator_code_version=GENERATOR_CODE_VERSION,
    )

    labels = {node.stable_key: node.label for node in revised.nodes}
    rebuilt = [
        NodeDecision(
            stable_key=node.stable_key,
            label=labels.get(node.stable_key, node.stable_key),
            decision=str(node.decision),
            reason_code=str(node.reason_code),
            reason=node.reason,
            provider_calls=node.provider_calls,
        )
        for node in plan.nodes
        if str(node.decision) == "REBUILD"
    ]

    return DemoProof(
        source="TEMPLATE_PROJECTION",
        verified_build=False,
        template=revised.template_version_label,
        total_nodes=len(revised.nodes),
        reuse=plan.summary.reuse,
        rebuild=plan.summary.rebuild,
        review=plan.summary.review,
        blocked=plan.summary.blocked,
        provider_calls=plan.summary.provider_calls,
        pricing_status=str(plan.summary.pricing_status or PricingStatus.UNKNOWN),
        estimated_cost_usd=plan.summary.estimated_cost_usd,
        change_from=ORIGINAL_LEGAL_LINE,
        change_to=REVISED_LEGAL_LINE,
        rebuild_nodes=rebuilt,
        plan_hash=plan.plan_hash,
        graph_hash=revised.canonical_hash,
    )


async def _poster_preview_url(
    session: AsyncSession, build_id: uuid.UUID, sign: Callable[[str], str]
) -> str | None:
    """Short-lived signed URL for the build's selected poster asset.

    §18.5 asks the landing page for a real ORBIT media preview. Returning None
    rather than a placeholder is deliberate: a page that shows stock art where it
    promises real output is the "unlabeled demo data on a live-looking path" that
    §0.1 forbids. No poster, no preview.
    """
    row = await session.execute(
        select(Asset.b2_key)
        .join(AttemptAsset, AttemptAsset.asset_id == Asset.id)
        .join(BuildNode, BuildNode.selected_attempt_id == AttemptAsset.attempt_id)
        .where(
            BuildNode.build_id == build_id,
            BuildNode.stable_key == POSTER_KEY,
            AttemptAsset.selected.is_(True),
            Asset.media_kind == "IMAGE",
            Asset.verified_at.is_not(None),
        )
        .limit(1)
    )
    key = row.scalar_one_or_none()
    return None if key is None else sign(key)


async def load_demo_proof(
    session: AsyncSession, *, sign: Callable[[str], str] | None = None
) -> DemoProof:
    """Prefer the latest proof event bound to a successful real demo build.

    `sign` is injected rather than constructed here so this module keeps no
    storage dependency and stays usable in tests without credentials.
    """
    event = await session.scalar(
        select(DomainEvent)
        .join(Build, Build.id == DomainEvent.build_id)
        .join(Project, Project.id == Build.project_id)
        .where(
            DomainEvent.event_type == "demo.proof.computed",
            Build.status == "SUCCEEDED",
            Build.is_fixture.is_(False),
            Project.is_demo.is_(True),
        )
        .order_by(DomainEvent.sequence.desc())
        .limit(1)
    )
    if event is None:
        return build_demo_proof()

    payload = dict(event.payload_json)
    payload["source"] = "BUILD_EVENTS"
    payload["verified_build"] = True
    if sign is not None and event.build_id is not None:
        payload["poster_url"] = await _poster_preview_url(session, event.build_id, sign)
    return DemoProof.model_validate(payload)
