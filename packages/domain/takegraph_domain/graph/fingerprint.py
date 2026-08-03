"""Node fingerprints (PRD §9.4, §12.2).

A fingerprint identifies an executable recipe and its exact selected inputs — not
the nondeterministic bytes a provider happens to produce. Two nodes with the same
fingerprint would run the same operation over the same inputs, which is precisely
the condition that makes reuse safe.

§12.2 is explicit about what must NOT be included: build ID, timestamps, database
UUIDs, transient signed URLs, attempt numbers. Any of those would destroy
legitimate reuse by making every fingerprint unique per build.
"""

from __future__ import annotations

from takegraph_domain.canonical import JsonValue, canonical_hash, canonical_payload
from takegraph_domain.graph.types import SCHEMA_VERSION, CompiledNode

PENDING_PREFIX = "pending:"
"""Marks an input whose producing node will rebuild, so its output bytes do not
exist yet. §12.4: "proposed selected outputs = unknown placeholders tied to
proposed fingerprint". The placeholder embeds the producer's proposed fingerprint,
so it differs from the previously selected asset hash and the invalidation
propagates — which is exactly how a rebuild cascades to descendants."""

GENERATOR_CODE_VERSION = "takegraph-generator-v1"
"""Version of executable node semantics included in every non-source fingerprint."""


def pending_output_ref(proposed_fingerprint: str) -> str:
    return f"{PENDING_PREFIX}{proposed_fingerprint}"


def is_pending(output_ref: str | None) -> bool:
    return output_ref is not None and output_ref.startswith(PENDING_PREFIX)


def compute_fingerprint(
    node: CompiledNode,
    *,
    input_refs: dict[str, str | None],
    generator_code_version: str,
    template_version: str,
) -> str:
    """Fingerprint one node.

    `input_refs` maps each declared input's producing node key to that node's
    selected output reference — a SHA-256 for durable media, a structured content
    hash for plans and copy, or a pending placeholder when the producer will
    rebuild.

    Inputs are emitted in the node's declared slot order, never sorted: §9.4
    requires preserving arrays whose order is semantically meaningful, and swapping
    two slots genuinely changes the operation.
    """
    ordered_inputs: list[JsonValue] = [
        {
            "slot": slot.slot,
            "ordinal": slot.ordinal,
            "asset_role": slot.asset_role,
            "selected_asset_sha256": input_refs.get(slot.from_key),
        }
        for slot in node.inputs
    ]

    payload: JsonValue = {
        "schema_version": SCHEMA_VERSION,
        "node_type": str(node.node_type),
        "normalized_operation": node.normalized_operation,
        "resolved_provider_policy_hash": node.provider_policy_hash,
        "validation_policy_hash": node.validation_policy_hash,
        "ordered_inputs": ordered_inputs,
        "generator_code_version": generator_code_version,
        "template_version": template_version,
    }
    return canonical_hash(canonical_payload(payload))


def compute_source_fingerprint(
    node: CompiledNode,
    *,
    content_hash: str,
    normalization_version: str = "1",
) -> str:
    """§12.2: "For source nodes, use the verified source content hash plus
    normalization/version metadata."

    A source has no upstream inputs, so its identity is the verified bytes (or
    normalized text) it carries. `content_hash` must come from bytes the system
    hashed itself — §8.3.7 forbids trusting a provider- or client-supplied claim.
    """
    return canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "node_type": str(node.node_type),
            "stable_key": node.stable_key,
            "content_hash": content_hash,
            "normalization_version": normalization_version,
            "normalized_operation": node.normalized_operation,
        }
    )
