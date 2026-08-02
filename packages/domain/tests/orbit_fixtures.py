"""Shared ORBIT builders for domain tests.

Kept out of conftest.py so tests can import these helpers by name; conftest.py
re-exports the pytest fixtures.
"""

from __future__ import annotations

import hashlib

from takegraph_domain.enums import BuildNodeStatus
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.fingerprint import compute_fingerprint, compute_source_fingerprint
from takegraph_domain.graph.orbit import (
    ORBIT_TEMPLATE,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
    REFERENCED_POLICIES,
)
from takegraph_domain.graph.types import CompiledGraph, NodeCacheState

GENERATOR_CODE_VERSION = "test-generator-1"

BRIEF_TEXT = (
    "ORBIT Hydration launch. Dark graphite set, crisp white bottle, teal orbital line, "
    "restrained orange accent. Four shots, cinematic."
)

#: Content hashes for the two source nodes. In production these are SHA-256 over
#: bytes the system downloaded and hashed itself (§8.3.7); here they are fixed so
#: the tests are deterministic.
SOURCE_CONTENT_HASHES = {
    "source.brief": hashlib.sha256(BRIEF_TEXT.encode()).hexdigest(),
    "source.product_reference": "a1" * 32,
}

#: Stand-in for "this policy version resolved to an immutable hash" (§12.1 step 7).
POLICY_HASHES = {key: hashlib.sha256(key.encode()).hexdigest() for key in REFERENCED_POLICIES}


def orbit_graph(*, legal_line: str = "zero sugar", brief_text: str = BRIEF_TEXT) -> CompiledGraph:
    return compile_graph(
        ORBIT_TEMPLATE,
        parameters={PARAM_LEGAL_LINE: legal_line, PARAM_BRIEF_TEXT: brief_text},
        policy_hashes=POLICY_HASHES,
    )


def completed_build_states(graph: CompiledGraph) -> dict[str, NodeCacheState]:
    """Model a baseline build in which every node passed.

    Walks topological order exactly as a real build would, recording each node's
    fingerprint and the output it selected. Generated nodes get a deterministic
    stand-in for "some bytes were produced and hashed" — the impact algorithm only
    ever compares these for equality, so their derivation is irrelevant as long as
    it is stable and distinct per node.
    """
    output_refs: dict[str, str | None] = {}
    states: dict[str, NodeCacheState] = {}

    for stable_key in graph.topological_order:
        node = graph.by_key[stable_key]
        if node.node_type.is_source:
            fingerprint = compute_source_fingerprint(
                node, content_hash=SOURCE_CONTENT_HASHES[stable_key]
            )
            selected = SOURCE_CONTENT_HASHES[stable_key]
        else:
            fingerprint = compute_fingerprint(
                node,
                input_refs=output_refs,
                generator_code_version=GENERATOR_CODE_VERSION,
                template_version=graph.template_version_label,
            )
            selected = hashlib.sha256(f"produced-bytes:{fingerprint}".encode()).hexdigest()

        output_refs[stable_key] = selected
        states[stable_key] = NodeCacheState(
            stable_key=stable_key,
            fingerprint=fingerprint,
            status=BuildNodeStatus.PASSED,
            selected_output_hash=selected,
            source_build_node_id=f"bn-{stable_key}",
        )
    return states
