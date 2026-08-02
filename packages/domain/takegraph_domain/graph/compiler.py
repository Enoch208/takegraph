"""Graph compiler (PRD §12.1).

Compiles a versioned template against a project revision into an immutable graph
snapshot. Rejects cycles, missing dependencies, duplicate stable keys, duplicate
input slots/ordinals, self-edges, and unresolvable parameter bindings.

Determinism is the contract: output must not depend on input ordering, so the
canonical hash is computed over nodes sorted by stable key while each node's own
input array keeps its authored order (§9.4 preserves semantically meaningful
array order).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence

from takegraph_domain.canonical import JsonValue, canonical_hash, canonical_payload
from takegraph_domain.enums import ApiErrorCode
from takegraph_domain.errors import DomainError
from takegraph_domain.graph.types import (
    SCHEMA_VERSION,
    CompiledGraph,
    CompiledNode,
    GraphTemplate,
    InputSlot,
    TemplateNode,
)

COMPILER_VERSION = "1"
"""Bumped when compilation semantics change. Participates in the graph hash and in
every fingerprint, so a bump correctly invalidates cached work (§12.5
TEMPLATE_VERSION_CHANGED / GENERATOR_CODE_CHANGED)."""


class GraphCompilationError(DomainError):
    error_code = ApiErrorCode.INVALID_GRAPH


class GraphCycleError(GraphCompilationError):
    error_code = ApiErrorCode.GRAPH_CYCLE


def compile_graph(
    template: GraphTemplate,
    *,
    parameters: Mapping[str, JsonValue] | None = None,
    policy_hashes: Mapping[str, str] | None = None,
) -> CompiledGraph:
    """Compile `template` against a revision's parameters.

    `policy_hashes` maps a provider/validation policy key to the canonical hash of
    its immutable resolved version (§12.1 step 7). A referenced policy with no hash
    is a compilation error rather than a silent None, because a missing policy would
    otherwise make two different configurations fingerprint identically.
    """
    parameters = parameters or {}
    policy_hashes = policy_hashes or {}

    _reject_duplicate_keys(template)
    known_keys = {node.stable_key for node in template.nodes}

    compiled: list[CompiledNode] = []
    for node in template.nodes:
        _validate_inputs(node.stable_key, node.inputs, known_keys)
        operation = _resolve_operation(node, parameters)
        compiled.append(
            CompiledNode(
                stable_key=node.stable_key,
                node_type=node.node_type,
                required=node.required,
                inputs=node.inputs,
                normalized_operation=operation,
                provider_policy_hash=_resolve_policy(
                    node.stable_key, "provider_policy", node.provider_policy, policy_hashes
                ),
                validation_policy_hash=_resolve_policy(
                    node.stable_key, "validation_policy", node.validation_policy, policy_hashes
                ),
                output_roles=node.output_roles,
                label=node.label or node.stable_key,
            )
        )

    order = topological_order(compiled)
    return CompiledGraph(
        template_key=template.key,
        template_version=template.version,
        compiler_version=COMPILER_VERSION,
        nodes=tuple(compiled),
        topological_order=order,
        canonical_hash=_graph_hash(template, compiled),
    )


def _reject_duplicate_keys(template: GraphTemplate) -> None:
    seen: set[str] = set()
    for node in template.nodes:
        if node.stable_key in seen:
            raise GraphCompilationError(f"duplicate stable key in template: {node.stable_key!r}")
        seen.add(node.stable_key)


def _validate_inputs(stable_key: str, inputs: tuple[InputSlot, ...], known_keys: set[str]) -> None:
    seen_slots: set[tuple[str, int]] = set()
    for slot in inputs:
        if slot.from_key == stable_key:
            raise GraphCompilationError(f"self-edge on node {stable_key!r}")
        if slot.from_key not in known_keys:
            raise GraphCompilationError(
                f"node {stable_key!r} depends on unknown node {slot.from_key!r}"
            )
        key = (slot.slot, slot.ordinal)
        if key in seen_slots:
            raise GraphCompilationError(
                f"node {stable_key!r} has duplicate input slot {slot.slot!r} ordinal {slot.ordinal}"
            )
        seen_slots.add(key)


def _resolve_operation(
    node: TemplateNode, parameters: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Apply the allow-listed parameter bindings, then freeze.

    Only keys named in `parameter_bindings` may be written, and only from the
    revision's `parameters` map — nothing else in the revision can reach a node
    operation. That restriction is what bounds the blast radius of an edit.
    """
    operation = dict(node.operation)
    for binding in node.parameter_bindings:
        if binding.parameter not in parameters:
            raise GraphCompilationError(
                f"node {node.stable_key!r} binds parameter {binding.parameter!r}, "
                "which the project revision does not define"
            )
        operation[binding.operation_key] = parameters[binding.parameter]
    canonical_payload(operation)  # fail loudly now, not at fingerprint time
    return operation


def _resolve_policy(
    stable_key: str, kind: str, key: str | None, policy_hashes: Mapping[str, str]
) -> str | None:
    if key is None:
        return None
    if key not in policy_hashes:
        raise GraphCompilationError(
            f"node {stable_key!r} references {kind} {key!r} with no resolved version"
        )
    return policy_hashes[key]


def topological_order(nodes: list[CompiledNode]) -> tuple[str, ...]:
    """Kahn's algorithm (§12.1 step 5), proving acyclicity and yielding a
    deterministic order.

    The ready set is drained in stable-key order so the result depends only on
    graph structure, never on list ordering.
    """
    indegree = {node.stable_key: 0 for node in nodes}
    dependents: dict[str, list[str]] = {node.stable_key: [] for node in nodes}

    for node in nodes:
        # A node may legitimately declare the same upstream in two slots; that is
        # one structural dependency, so dedupe before counting indegree.
        for upstream in sorted({slot.from_key for slot in node.inputs}):
            indegree[node.stable_key] += 1
            dependents[upstream].append(node.stable_key)

    ready = deque(sorted(key for key, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while ready:
        key = ready.popleft()
        order.append(key)
        newly_ready = []
        for dependent in dependents[key]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly_ready.append(dependent)
        for dependent in sorted(newly_ready):
            ready.append(dependent)
        # Keep the frontier sorted so ties resolve identically on every run.
        ready = deque(sorted(ready))

    if len(order) != len(nodes):
        unresolved = sorted(set(indegree) - set(order))
        raise GraphCycleError(f"graph contains a cycle among nodes: {', '.join(unresolved)}")
    return tuple(order)


def _graph_hash(template: GraphTemplate, nodes: list[CompiledNode]) -> str:
    """§12.1 step 8. Sorted by stable key so insertion order cannot change it."""
    return canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "compiler_version": COMPILER_VERSION,
            "template_key": template.key,
            "template_version": template.version,
            "nodes": [
                {"stable_key": node.stable_key, "spec_hash": node.spec_hash}
                for node in sorted(nodes, key=lambda n: n.stable_key)
            ],
        }
    )


def assert_reaches(graph: CompiledGraph, *, delivery_keys: Sequence[str]) -> None:
    """§12.1 step 6: verify every required node feeds at least one deliverable.

    Deliverables are plural on purpose. In the ORBIT graph `image.poster` is a
    required output (§4.1 lists a poster thumbnail) but is not an input to
    `compose.delivery_package`, whose dependencies are nodes 9–16. Treating a
    single node as "the" delivery target would wrongly flag the poster as orphaned.
    """
    by_key = graph.by_key
    missing = [key for key in delivery_keys if key not in by_key]
    if missing:
        raise GraphCompilationError(f"delivery nodes not in graph: {', '.join(sorted(missing))}")

    reaching: set[str] = set(delivery_keys)
    frontier = deque(delivery_keys)
    while frontier:
        key = frontier.popleft()
        for slot in by_key[key].inputs:
            if slot.from_key not in reaching:
                reaching.add(slot.from_key)
                frontier.append(slot.from_key)

    orphaned = sorted(
        node.stable_key for node in graph.nodes if node.required and node.stable_key not in reaching
    )
    if orphaned:
        raise GraphCompilationError(
            f"required nodes reach no deliverable ({', '.join(sorted(delivery_keys))}): "
            f"{', '.join(orphaned)}"
        )
