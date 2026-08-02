"""AS-01 and §22.2: the legal-copy change must invalidate exactly four nodes.

This is the product's central claim. If it regresses, TAKEGRAPH's entire promise —
"tell me exactly what is affected, keep everything still valid" — is false, so
these tests are the ones to keep green above all others.
"""

from __future__ import annotations

from orbit_fixtures import GENERATOR_CODE_VERSION, SOURCE_CONTENT_HASHES, orbit_graph
from takegraph_domain.enums import ImpactDecision, PricingStatus, ReasonCode
from takegraph_domain.graph.impact import compute_impact
from takegraph_domain.graph.orbit import EXPECTED_LEGAL_COPY_REBUILD
from takegraph_domain.graph.types import CompiledGraph, NodeCacheState


def plan_for(graph: CompiledGraph, states: dict[str, NodeCacheState]):
    return compute_impact(
        graph,
        base_states=states,
        source_content_hashes=SOURCE_CONTENT_HASHES,
        generator_code_version=GENERATOR_CODE_VERSION,
    )


class TestLegalCopyChange:
    """§4.2: `zero sugar` -> `no added sugar` invalidates exactly copy.pack,
    audio.narration, graphic.end_card and compose.delivery_package."""

    def test_rebuilds_exactly_the_four_named_nodes(self, baseline_states) -> None:
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)

        assert plan.rebuild_keys == EXPECTED_LEGAL_COPY_REBUILD, (
            f"expected exactly {list(EXPECTED_LEGAL_COPY_REBUILD)}, got {list(plan.rebuild_keys)}"
        )

    def test_summary_is_fourteen_reuse_four_rebuild(self, baseline_states) -> None:
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)

        assert plan.summary.reuse == 14
        assert plan.summary.rebuild == 4
        assert plan.summary.review == 0
        assert plan.summary.blocked == 0
        assert plan.summary.reuse + plan.summary.rebuild == 18

    def test_every_other_node_reuses(self, baseline_states) -> None:
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)

        rebuilt = set(EXPECTED_LEGAL_COPY_REBUILD)
        for node in plan.nodes:
            if node.stable_key not in rebuilt:
                assert node.decision is ImpactDecision.REUSE, (
                    f"{node.stable_key} should reuse, got {node.decision} ({node.reason})"
                )
                assert node.reason_code is ReasonCode.EXACT_VALIDATED_REUSE

    def test_shots_keyframes_music_cutout_and_poster_survive(self, baseline_states) -> None:
        """§4.2: "All shots, keyframes, music, product cutout, and poster remain
        reusable." The poster matters most — it descends from keyframe.01, so a
        naive "invalidate everything downstream of any change" would wrongly drop it."""
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)

        must_survive = {
            "transform.product_cutout",
            "plan.shots",
            "image.keyframe.01",
            "image.keyframe.02",
            "image.keyframe.03",
            "image.keyframe.04",
            "video.clip.01",
            "video.clip.02",
            "video.clip.03",
            "video.clip.04",
            "audio.music",
            "image.poster",
        }
        assert must_survive <= set(plan.reuse_keys)

    def test_reason_codes_distinguish_origin_from_cascade(self, baseline_states) -> None:
        """§5.3 FR-IMPACT-002: every non-reuse decision carries a machine code and a
        human-readable reason. The node the user actually edited must read
        differently from the three that were dragged along."""
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)
        reasons = {n.stable_key: n for n in plan.nodes}

        assert reasons["copy.pack"].reason_code is ReasonCode.NODE_SPEC_CHANGED
        for cascaded in ("audio.narration", "graphic.end_card", "compose.delivery_package"):
            assert reasons[cascaded].reason_code is ReasonCode.UPSTREAM_FINGERPRINT_CHANGED
            assert "copy.pack" in reasons[cascaded].reason

    def test_fingerprints_change_only_for_rebuilt_nodes(self, baseline_states) -> None:
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)

        for node in plan.nodes:
            old = baseline_states[node.stable_key].fingerprint
            if node.stable_key in set(EXPECTED_LEGAL_COPY_REBUILD):
                assert node.new_fingerprint != old, f"{node.stable_key} fingerprint should differ"
            else:
                assert node.new_fingerprint == old, f"{node.stable_key} fingerprint should match"

    def test_provider_calls_counted_only_for_generative_rebuilds(self, baseline_states) -> None:
        """copy.pack and audio.narration call providers. graphic.end_card and
        compose.delivery_package run locally, so promising provider calls for them
        would overstate the cost of the change."""
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)
        calls = {n.stable_key: n.provider_calls for n in plan.nodes}

        assert calls["copy.pack"] == 1
        assert calls["audio.narration"] == 1
        assert calls["graphic.end_card"] == 0
        assert calls["compose.delivery_package"] == 0
        assert plan.summary.provider_calls == 2

    def test_pricing_stays_unknown_never_zero(self, baseline_states) -> None:
        """§5.3 FR-IMPACT-003: "Unknown pricing remains unknown, not zero.""" ""
        revised = orbit_graph(legal_line="no added sugar")
        plan = plan_for(revised, baseline_states)

        assert plan.summary.pricing_status is PricingStatus.UNKNOWN
        assert plan.summary.estimated_cost_usd is None
        assert all(n.estimated_cost_usd is None for n in plan.nodes)


class TestNoChange:
    def test_identical_revision_reuses_everything(self, baseline_graph, baseline_states) -> None:
        plan = plan_for(baseline_graph, baseline_states)
        assert plan.summary.reuse == 18
        assert plan.summary.rebuild == 0
        assert plan.summary.provider_calls == 0


class TestDeterminism:
    """§5.3 FR-IMPACT-005: identical inputs return identical impact plans."""

    def test_plan_hash_is_stable_across_runs(self, baseline_states) -> None:
        a = plan_for(orbit_graph(legal_line="no added sugar"), baseline_states)
        b = plan_for(orbit_graph(legal_line="no added sugar"), baseline_states)
        assert a.plan_hash == b.plan_hash

    def test_plan_hash_changes_with_the_edit(self, baseline_states) -> None:
        a = plan_for(orbit_graph(legal_line="no added sugar"), baseline_states)
        b = plan_for(orbit_graph(legal_line="sugar free"), baseline_states)
        assert a.plan_hash != b.plan_hash

    def test_plan_hash_is_bound_to_the_graph(self, baseline_graph, baseline_states) -> None:
        """§11.5 returns IMPACT_PLAN_STALE when the graph changed since preview, so
        the hash must move when the graph does."""
        plan = plan_for(baseline_graph, baseline_states)
        assert plan.graph_canonical_hash == baseline_graph.canonical_hash


class TestCascadeIsConservative:
    """§12.4: "all descendants that consume its output are conservatively invalid"."""

    def test_changing_a_source_invalidates_its_whole_cone(self, baseline_states) -> None:
        """Replacing the product reference image touches nearly everything, and it
        should — the opposite failure (over-eager reuse) would ship stale media."""
        revised = orbit_graph()
        changed_sources = dict(SOURCE_CONTENT_HASHES, **{"source.product_reference": "b2" * 32})
        plan = compute_impact(
            revised,
            base_states=baseline_states,
            source_content_hashes=changed_sources,
            generator_code_version=GENERATOR_CODE_VERSION,
        )

        rebuilt = set(plan.rebuild_keys)
        assert "source.product_reference" in rebuilt
        assert {"transform.product_cutout", "plan.shots", "image.poster"} <= rebuilt
        assert all(f"image.keyframe.{i:02d}" in rebuilt for i in range(1, 5))
        assert all(f"video.clip.{i:02d}" in rebuilt for i in range(1, 5))
        # source.brief has no dependency on the image, so it must survive.
        assert "source.brief" in set(plan.reuse_keys)

    def test_generator_code_change_invalidates_generated_nodes(self, baseline_states) -> None:
        """§12.5 GENERATOR_CODE_CHANGED. Sources are content-addressed and unaffected."""
        plan = compute_impact(
            orbit_graph(),
            base_states=baseline_states,
            source_content_hashes=SOURCE_CONTENT_HASHES,
            generator_code_version="different-generator-2",
        )
        assert plan.summary.rebuild == 16
        assert set(plan.reuse_keys) == {"source.brief", "source.product_reference"}
