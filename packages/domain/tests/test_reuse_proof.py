"""Reuse proof and invalidation invariants — PRD §12.3 and the §22.2 property tests.

Two failure modes matter here and they are not symmetric. Under-reuse costs money
and time. Over-reuse ships stale or unverifiable media into a release that claims
provenance, which is the failure the whole product exists to prevent. These tests
lean on the second.
"""

from __future__ import annotations

import pytest
from orbit_fixtures import (
    GENERATOR_CODE_VERSION,
    POLICY_HASHES,
    SOURCE_CONTENT_HASHES,
    completed_build_states,
    orbit_graph,
)
from takegraph_domain.enums import BuildNodeStatus, ImpactDecision, ReasonCode
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.impact import compute_impact, evaluate_reuse_proof
from takegraph_domain.graph.orbit import ORBIT_TEMPLATE, PARAM_BRIEF_TEXT, PARAM_LEGAL_LINE
from takegraph_domain.graph.types import CompiledGraph, NodeCacheState

ALL_KEYS = tuple(orbit_graph().topological_order)
GENERATED_KEYS = tuple(k for k in ALL_KEYS if not k.startswith("source."))


def transitive_dependents(graph: CompiledGraph, key: str) -> set[str]:
    seen: set[str] = set()
    frontier = [key]
    while frontier:
        current = frontier.pop()
        for dependent in graph.dependents_of(current):
            if dependent not in seen:
                seen.add(dependent)
                frontier.append(dependent)
    return seen


def plan(graph: CompiledGraph, states: dict[str, NodeCacheState]):
    return compute_impact(
        graph,
        base_states=states,
        source_content_hashes=SOURCE_CONTENT_HASHES,
        generator_code_version=GENERATOR_CODE_VERSION,
    )


class TestReuseProofRejections:
    """§12.3 enumerates the conditions. Each must independently block reuse."""

    def _candidate(self, **overrides) -> NodeCacheState:
        base = {
            "stable_key": "video.clip.01",
            "fingerprint": "fp-exact",
            "status": BuildNodeStatus.PASSED,
            "selected_output_hash": "cd" * 32,
        }
        return NodeCacheState(**{**base, **overrides})

    def test_exact_match_is_reusable(self) -> None:
        assert (
            evaluate_reuse_proof(proposed_fingerprint="fp-exact", candidate=self._candidate())
            is None
        )

    def test_no_candidate_is_a_cache_miss(self) -> None:
        rejection = evaluate_reuse_proof(proposed_fingerprint="fp-exact", candidate=None)
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.CACHE_MISS

    def test_fingerprint_mismatch_blocks_reuse(self) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-different", candidate=self._candidate()
        )
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.NODE_SPEC_CHANGED

    def test_missing_asset_blocks_reuse(self) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-exact", candidate=self._candidate(assets_present=False)
        )
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.CACHE_ASSET_MISSING

    def test_unverified_bytes_block_reuse(self) -> None:
        """§8.3.6: an asset is not durable until stored bytes match the recorded hash."""
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-exact", candidate=self._candidate(assets_verified=False)
        )
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.CACHE_ASSET_UNVERIFIED

    def test_stale_validation_blocks_reuse(self) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-exact", candidate=self._candidate(validations_current=False)
        )
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.CACHE_VALIDATION_STALE

    def test_revoked_output_blocks_reuse(self) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-exact", candidate=self._candidate(revoked=True)
        )
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.MANUAL_INVALIDATION

    @pytest.mark.parametrize(
        "status",
        [
            BuildNodeStatus.FAILED,
            BuildNodeStatus.WAITING_REVIEW,
            BuildNodeStatus.CANCELLED,
            BuildNodeStatus.RUNNING,
        ],
    )
    def test_unaccepted_status_blocks_reuse(self, status: BuildNodeStatus) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-exact", candidate=self._candidate(status=status)
        )
        assert rejection is not None

    def test_manually_approved_node_is_reusable(self) -> None:
        """§12.3: "previously reached PASSED or was explicitly approved"."""
        candidate = self._candidate(status=BuildNodeStatus.WAITING_REVIEW, manually_approved=True)
        assert evaluate_reuse_proof(proposed_fingerprint="fp-exact", candidate=candidate) is None

    def test_selected_output_is_required(self) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp-exact", candidate=self._candidate(selected_output_hash=None)
        )
        assert rejection is not None
        assert rejection.reason_code is ReasonCode.CACHE_ASSET_MISSING


class TestFixtureIsolation:
    """§12.3 and §0.1: fixture output must not leak onto a path presented as live."""

    def _fixture_candidate(self) -> NodeCacheState:
        return NodeCacheState(
            stable_key="video.clip.01",
            fingerprint="fp",
            status=BuildNodeStatus.PASSED,
            selected_output_hash="ab" * 32,
            is_fixture=True,
        )

    def test_fixture_is_not_reusable_by_a_live_build(self) -> None:
        rejection = evaluate_reuse_proof(
            proposed_fingerprint="fp", candidate=self._fixture_candidate()
        )
        assert rejection is not None
        assert "fixture" in rejection.reason.lower()

    def test_fixture_is_reusable_by_a_fixture_scoped_build(self) -> None:
        assert (
            evaluate_reuse_proof(
                proposed_fingerprint="fp",
                candidate=self._fixture_candidate(),
                allow_fixture_reuse=True,
            )
            is None
        )


class TestInvalidationInvariants:
    """§22.2 property tests, run exhaustively over all 18 nodes rather than sampled —
    the graph is fixed and small enough to cover completely."""

    @pytest.mark.parametrize("key", GENERATED_KEYS)
    def test_no_reuse_when_any_fingerprint_input_differs(self, key: str) -> None:
        """ "No node marked reuse when any fingerprint input differs." Perturbing a
        cached fingerprint must make that node rebuild, whatever else is true."""
        graph = orbit_graph()
        states = completed_build_states(graph)
        states[key] = states[key].model_copy(update={"fingerprint": "0" * 64})

        result = plan(graph, states)
        decision = next(n for n in result.nodes if n.stable_key == key)
        assert decision.decision is ImpactDecision.REBUILD

    @pytest.mark.parametrize("key", GENERATED_KEYS)
    def test_affected_set_contains_every_reachable_dependent(self, key: str) -> None:
        """ "Affected set contains every reachable dependent unless an output-slot
        exemption is explicit." No such exemption exists in this template, so the
        rebuild set must be closed under reachability."""
        graph = orbit_graph()
        states = completed_build_states(graph)
        states[key] = states[key].model_copy(update={"revoked": True})

        result = plan(graph, states)
        rebuilt = set(result.rebuild_keys)
        expected = transitive_dependents(graph, key) | {key}
        assert expected <= rebuilt, f"missing dependents of {key}: {sorted(expected - rebuilt)}"

    @pytest.mark.parametrize("key", GENERATED_KEYS)
    def test_invalidation_does_not_over_reach(self, key: str) -> None:
        """The other half of the promise: "keep everything still valid". Nothing
        outside the node and its descendants may be dragged into a rebuild."""
        graph = orbit_graph()
        states = completed_build_states(graph)
        states[key] = states[key].model_copy(update={"revoked": True})

        result = plan(graph, states)
        allowed = transitive_dependents(graph, key) | {key}
        assert set(result.rebuild_keys) == allowed


class TestPolicyChangesInvalidate:
    """§12.5 PROVIDER_POLICY_CHANGED / VALIDATION_POLICY_CHANGED."""

    def _graph_with_policy(self, policy_key: str, new_hash: str) -> CompiledGraph:
        return compile_graph(
            ORBIT_TEMPLATE,
            parameters={PARAM_LEGAL_LINE: "zero sugar", PARAM_BRIEF_TEXT: "brief"},
            policy_hashes={**POLICY_HASHES, policy_key: new_hash},
        )

    def test_provider_policy_bump_invalidates_only_its_nodes(self) -> None:
        """§5.5 FR-PROV-001: "Changing policy changes node fingerprint." Only the
        video nodes use orbit-video-v1, so only they and their descendants move."""
        baseline = compile_graph(
            ORBIT_TEMPLATE,
            parameters={PARAM_LEGAL_LINE: "zero sugar", PARAM_BRIEF_TEXT: "brief"},
            policy_hashes=POLICY_HASHES,
        )
        states = completed_build_states(baseline)
        revised = self._graph_with_policy("orbit-video-v1", "f" * 64)

        result = compute_impact(
            revised,
            base_states=states,
            source_content_hashes=SOURCE_CONTENT_HASHES,
            generator_code_version=GENERATOR_CODE_VERSION,
        )
        rebuilt = set(result.rebuild_keys)
        assert {f"video.clip.{i:02d}" for i in range(1, 5)} <= rebuilt
        assert "compose.delivery_package" in rebuilt
        # Keyframes are upstream of the clips and use a different policy.
        assert all(f"image.keyframe.{i:02d}" in set(result.reuse_keys) for i in range(1, 5))
        assert "image.poster" in set(result.reuse_keys)

    def test_validation_policy_bump_invalidates(self) -> None:
        baseline = compile_graph(
            ORBIT_TEMPLATE,
            parameters={PARAM_LEGAL_LINE: "zero sugar", PARAM_BRIEF_TEXT: "brief"},
            policy_hashes=POLICY_HASHES,
        )
        states = completed_build_states(baseline)
        revised = self._graph_with_policy("orbit-copy-qc-v1", "e" * 64)

        result = compute_impact(
            revised,
            base_states=states,
            source_content_hashes=SOURCE_CONTENT_HASHES,
            generator_code_version=GENERATOR_CODE_VERSION,
        )
        assert "copy.pack" in set(result.rebuild_keys)


class TestBlockedNodes:
    """§12.4: a node that cannot execute is BLOCKED, not optimistically REBUILD."""

    def test_blocked_node_is_reported_not_promised(self) -> None:
        graph = orbit_graph(legal_line="no added sugar")
        states = completed_build_states(orbit_graph(legal_line="zero sugar"))

        result = compute_impact(
            graph,
            base_states=states,
            source_content_hashes=SOURCE_CONTENT_HASHES,
            generator_code_version=GENERATOR_CODE_VERSION,
            blocked_keys={"audio.narration": "ELEVENLABS_API_KEY is not configured."},
        )
        narration = next(n for n in result.nodes if n.stable_key == "audio.narration")
        assert narration.decision is ImpactDecision.BLOCKED
        assert narration.reason_code is ReasonCode.CONFIGURATION_BLOCKED
        assert narration.provider_calls == 0
        assert narration.requires_human_review is True
        assert result.summary.blocked == 1

    def test_dependents_of_a_blocked_node_still_rebuild(self) -> None:
        """The delivery package consumes narration, so it cannot be reused even
        though narration itself will not run."""
        graph = orbit_graph(legal_line="no added sugar")
        states = completed_build_states(orbit_graph(legal_line="zero sugar"))

        result = compute_impact(
            graph,
            base_states=states,
            source_content_hashes=SOURCE_CONTENT_HASHES,
            generator_code_version=GENERATOR_CODE_VERSION,
            blocked_keys={"audio.narration": "ELEVENLABS_API_KEY is not configured."},
        )
        assert "compose.delivery_package" in set(result.rebuild_keys)


class TestSourceHashRequired:
    def test_missing_source_hash_fails_loudly(self) -> None:
        """A source with no verified content hash cannot be fingerprinted. Defaulting
        it would silently make every downstream node look reusable."""
        graph = orbit_graph()
        with pytest.raises(ValueError, match="no content hash"):
            compute_impact(
                graph,
                base_states={},
                source_content_hashes={"source.brief": "ab" * 32},
                generator_code_version=GENERATOR_CODE_VERSION,
            )
