"""Graph compiler tests — PRD §12.1 and the §22.2 mandatory list.

A compiler that silently accepts a malformed graph produces a build that deadlocks
or, worse, one that quietly drops a required deliverable. Every rejection here is
a failure the system must refuse at compile time rather than discover at runtime.
"""

from __future__ import annotations

import pytest
from orbit_fixtures import POLICY_HASHES, orbit_graph
from takegraph_domain.enums import ApiErrorCode, NodeType
from takegraph_domain.graph.compiler import (
    GraphCompilationError,
    GraphCycleError,
    assert_reaches,
    compile_graph,
)
from takegraph_domain.graph.orbit import DELIVERABLE_KEYS, ORBIT_TEMPLATE
from takegraph_domain.graph.types import GraphTemplate, InputSlot, ParameterBinding, TemplateNode


def node(key: str, *deps: str, node_type: NodeType = NodeType.IMAGE_GENERATION) -> TemplateNode:
    return TemplateNode(
        stable_key=key,
        node_type=node_type,
        inputs=tuple(InputSlot(slot=f"in{i}", from_key=d) for i, d in enumerate(deps)),
    )


def template(*nodes: TemplateNode) -> GraphTemplate:
    return GraphTemplate(key="t", version=1, nodes=nodes)


class TestStructuralRejection:
    def test_rejects_cycle(self) -> None:
        with pytest.raises(GraphCycleError) as excinfo:
            compile_graph(template(node("a", "c"), node("b", "a"), node("c", "b")))
        assert excinfo.value.error_code is ApiErrorCode.GRAPH_CYCLE
        assert {"a", "b", "c"} <= set(str(excinfo.value).replace(",", "").split())

    def test_rejects_self_edge(self) -> None:
        with pytest.raises(GraphCompilationError, match="self-edge"):
            compile_graph(template(node("a", "a")))

    def test_rejects_missing_dependency(self) -> None:
        with pytest.raises(GraphCompilationError, match="unknown node 'ghost'"):
            compile_graph(template(node("a", "ghost")))

    def test_rejects_duplicate_stable_key(self) -> None:
        with pytest.raises(GraphCompilationError, match="duplicate stable key"):
            compile_graph(template(node("a"), node("a")))

    def test_rejects_duplicate_input_slot_and_ordinal(self) -> None:
        dup = TemplateNode(
            stable_key="b",
            node_type=NodeType.MEDIA_COMPOSITION,
            inputs=(
                InputSlot(slot="clip", from_key="a", ordinal=0),
                InputSlot(slot="clip", from_key="a", ordinal=0),
            ),
        )
        with pytest.raises(GraphCompilationError, match="duplicate input slot"):
            compile_graph(template(node("a"), dup))

    def test_allows_same_upstream_in_distinct_ordinals(self) -> None:
        """A composition may legitimately take the same clip twice in different
        positions; only slot+ordinal collisions are invalid."""
        ok = TemplateNode(
            stable_key="b",
            node_type=NodeType.MEDIA_COMPOSITION,
            inputs=(
                InputSlot(slot="clip", from_key="a", ordinal=0),
                InputSlot(slot="clip", from_key="a", ordinal=1),
            ),
        )
        graph = compile_graph(template(node("a"), ok))
        assert graph.topological_order == ("a", "b")


class TestParameterBindings:
    def test_binding_writes_into_the_operation(self) -> None:
        tpl = template(
            TemplateNode(
                stable_key="a",
                node_type=NodeType.STRUCTURED_TEXT,
                parameter_bindings=(ParameterBinding(operation_key="phrase", parameter="legal"),),
            )
        )
        graph = compile_graph(tpl, parameters={"legal": "no added sugar"})
        assert graph.by_key["a"].normalized_operation["phrase"] == "no added sugar"

    def test_missing_parameter_is_a_compile_error(self) -> None:
        """Defaulting a missing parameter would let two different revisions compile
        to the same fingerprint, so this must fail loudly."""
        tpl = template(
            TemplateNode(
                stable_key="a",
                node_type=NodeType.STRUCTURED_TEXT,
                parameter_bindings=(ParameterBinding(operation_key="phrase", parameter="legal"),),
            )
        )
        with pytest.raises(GraphCompilationError, match="does not define"):
            compile_graph(tpl, parameters={})

    def test_unbound_parameters_cannot_reach_the_operation(self) -> None:
        """The allow-list is the blast-radius control for AS-01: a parameter no node
        binds must not appear anywhere in the compiled graph."""
        graph = orbit_graph()
        for compiled in graph.nodes:
            if compiled.stable_key != "copy.pack":
                assert "required_legal_phrase" not in compiled.normalized_operation


class TestPolicyResolution:
    def test_unresolved_policy_is_rejected(self) -> None:
        """§12.1 step 7. A None policy hash would make two different provider
        configurations fingerprint identically."""
        tpl = template(
            TemplateNode(
                stable_key="a", node_type=NodeType.VIDEO_GENERATION, provider_policy="missing-v1"
            )
        )
        with pytest.raises(GraphCompilationError, match="no resolved version"):
            compile_graph(tpl, policy_hashes={})


class TestDeterminism:
    def test_hash_is_independent_of_node_ordering(self) -> None:
        """§12.1: "Compiler output must be independent of database insertion order.""" ""
        forward = compile_graph(template(node("a"), node("b", "a"), node("c", "b")))
        shuffled = compile_graph(template(node("c", "b"), node("a"), node("b", "a")))
        assert forward.canonical_hash == shuffled.canonical_hash
        assert forward.topological_order == shuffled.topological_order

    def test_orbit_compiles_identically_every_time(self) -> None:
        assert orbit_graph().canonical_hash == orbit_graph().canonical_hash

    def test_hash_changes_when_a_parameter_changes(self) -> None:
        a = orbit_graph(legal_line="zero sugar")
        b = orbit_graph(legal_line="no added sugar")
        assert a.canonical_hash != b.canonical_hash

    def test_topological_order_breaks_ties_deterministically(self) -> None:
        graph = compile_graph(template(node("z"), node("y"), node("x")))
        assert graph.topological_order == ("x", "y", "z")


class TestOrbitSeedGraph:
    """§4.2: the seed template has exactly 18 nodes with the specified dependencies."""

    def test_has_exactly_eighteen_nodes(self) -> None:
        assert len(ORBIT_TEMPLATE.nodes) == 18

    def test_dependencies_match_the_prd_table(self) -> None:
        expected = {
            "source.brief": set(),
            "source.product_reference": set(),
            "transform.product_cutout": {"source.product_reference"},
            "plan.shots": {"source.brief", "source.product_reference"},
            "image.keyframe.01": {
                "source.product_reference",
                "transform.product_cutout",
                "plan.shots",
            },
            "image.keyframe.02": {
                "source.product_reference",
                "transform.product_cutout",
                "plan.shots",
            },
            "image.keyframe.03": {
                "source.product_reference",
                "transform.product_cutout",
                "plan.shots",
            },
            "image.keyframe.04": {
                "source.product_reference",
                "transform.product_cutout",
                "plan.shots",
            },
            "video.clip.01": {"plan.shots", "image.keyframe.01"},
            "video.clip.02": {"plan.shots", "image.keyframe.02"},
            "video.clip.03": {"plan.shots", "image.keyframe.03"},
            "video.clip.04": {"plan.shots", "image.keyframe.04"},
            "audio.music": {"source.brief", "plan.shots"},
            "copy.pack": {"source.brief"},
            "audio.narration": {"copy.pack"},
            "graphic.end_card": {"source.product_reference", "copy.pack"},
            "image.poster": {"source.product_reference", "image.keyframe.01"},
            "compose.delivery_package": {
                "video.clip.01",
                "video.clip.02",
                "video.clip.03",
                "video.clip.04",
                "audio.music",
                "copy.pack",
                "audio.narration",
                "graphic.end_card",
            },
        }
        actual = {n.stable_key: {slot.from_key for slot in n.inputs} for n in orbit_graph().nodes}
        assert actual == expected

    def test_compiles_and_is_acyclic(self) -> None:
        graph = orbit_graph()
        assert len(graph.topological_order) == 18

    def test_topological_order_respects_every_edge(self) -> None:
        graph = orbit_graph()
        position = {key: i for i, key in enumerate(graph.topological_order)}
        for compiled in graph.nodes:
            for slot in compiled.inputs:
                assert position[slot.from_key] < position[compiled.stable_key], (
                    f"{slot.from_key} must precede {compiled.stable_key}"
                )

    def test_every_required_node_reaches_a_deliverable(self) -> None:
        assert_reaches(orbit_graph(), delivery_keys=DELIVERABLE_KEYS)

    def test_poster_alone_is_not_a_sufficient_delivery_target(self) -> None:
        """Guards the plural-deliverables decision: checking only the delivery
        package would wrongly report the poster's ancestors as orphaned, and
        checking only the poster would orphan the clips."""
        with pytest.raises(GraphCompilationError, match="reach no deliverable"):
            assert_reaches(orbit_graph(), delivery_keys=("image.poster",))

    def test_all_referenced_policies_resolve(self) -> None:
        graph = orbit_graph()
        hashes = set(POLICY_HASHES.values())
        for compiled in graph.nodes:
            for policy_hash in (compiled.provider_policy_hash, compiled.validation_policy_hash):
                assert policy_hash is None or policy_hash in hashes
