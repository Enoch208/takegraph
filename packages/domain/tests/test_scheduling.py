"""Scheduling and completion tests — PRD §12.6 and §5.4 FR-BUILD-008.

Two ways a build lies. It can report SUCCEEDED while a required node failed, or
it can hang forever with nothing runnable and no explanation. The completion
tests target the first; the readiness tests target the second.
"""

from __future__ import annotations

from orbit_fixtures import orbit_graph
from takegraph_domain.builds.scheduling import (
    evaluate_build_completion,
    node_priority,
    ready_nodes,
)
from takegraph_domain.enums import BuildNodeStatus, BuildStatus, NodeType

SOURCES = ("source.brief", "source.product_reference")


def statuses(**overrides: BuildNodeStatus) -> dict[str, BuildNodeStatus]:
    """All 18 nodes PENDING unless overridden. Keys use the real stable keys."""
    graph = orbit_graph()
    base = {key: BuildNodeStatus.PENDING for key in graph.topological_order}
    base.update({k.replace("__", "."): v for k, v in overrides.items()})
    return base


def all_passed_except(*pending: str) -> dict[str, BuildNodeStatus]:
    graph = orbit_graph()
    return {
        key: (BuildNodeStatus.PENDING if key in pending else BuildNodeStatus.PASSED)
        for key in graph.topological_order
    }


class TestPriority:
    def test_control_nodes_outrank_media(self) -> None:
        """§12.6: cheap planning and copy work unblocks downstream media, so it
        goes first when dependencies allow."""
        assert node_priority(NodeType.STRUCTURED_TEXT) > node_priority(NodeType.VIDEO_GENERATION)
        assert node_priority(NodeType.STRUCTURED_PLAN) > node_priority(NodeType.IMAGE_GENERATION)

    def test_video_outranks_final_composition(self) -> None:
        """Composition consumes every clip, so it can never usefully run early."""
        assert node_priority(NodeType.VIDEO_GENERATION) > node_priority(NodeType.MEDIA_COMPOSITION)


class TestReadiness:
    def test_only_sources_are_ready_at_the_start(self) -> None:
        graph = orbit_graph()
        ready = ready_nodes(graph, node_statuses=statuses(), build_status=BuildStatus.RUNNING)
        assert {n.stable_key for n in ready} == set(SOURCES)

    def test_a_node_waits_for_every_dependency(self) -> None:
        """plan.shots needs both sources. One is not enough."""
        graph = orbit_graph()
        ready = ready_nodes(
            graph,
            node_statuses=statuses(source__brief=BuildNodeStatus.PASSED),
            build_status=BuildStatus.RUNNING,
        )
        assert "plan.shots" not in {n.stable_key for n in ready}

    def test_node_becomes_ready_when_all_dependencies_are_satisfied(self) -> None:
        graph = orbit_graph()
        ready = ready_nodes(
            graph,
            node_statuses=statuses(
                source__brief=BuildNodeStatus.PASSED,
                source__product_reference=BuildNodeStatus.PASSED,
            ),
            build_status=BuildStatus.RUNNING,
        )
        keys = {n.stable_key for n in ready}
        assert "plan.shots" in keys
        assert "copy.pack" in keys
        assert "transform.product_cutout" in keys

    def test_reused_dependency_unblocks_downstream(self) -> None:
        """A reused node satisfies dependencies exactly as a passed one does —
        that is what makes incremental builds work at all."""
        graph = orbit_graph()
        ready = ready_nodes(
            graph,
            node_statuses=statuses(
                source__brief=BuildNodeStatus.REUSED,
                source__product_reference=BuildNodeStatus.REUSED,
            ),
            build_status=BuildStatus.RUNNING,
        )
        assert "plan.shots" in {n.stable_key for n in ready}

    def test_waiting_review_does_not_unblock_downstream(self) -> None:
        """§5.4 FR-BUILD-007: unverified output must not satisfy a dependency."""
        graph = orbit_graph()
        ready = ready_nodes(
            graph,
            node_statuses=statuses(
                source__brief=BuildNodeStatus.PASSED,
                source__product_reference=BuildNodeStatus.WAITING_REVIEW,
            ),
            build_status=BuildStatus.RUNNING,
        )
        assert "plan.shots" not in {n.stable_key for n in ready}

    def test_higher_priority_nodes_come_first(self) -> None:
        graph = orbit_graph()
        ready = ready_nodes(
            graph,
            node_statuses=statuses(
                source__brief=BuildNodeStatus.PASSED,
                source__product_reference=BuildNodeStatus.PASSED,
            ),
            build_status=BuildStatus.RUNNING,
        )
        priorities = [n.priority for n in ready]
        assert priorities == sorted(priorities, reverse=True)

    def test_already_scheduled_nodes_are_not_offered_again(self) -> None:
        """§12.6: no active work item may already exist for the node."""
        graph = orbit_graph()
        ready = ready_nodes(
            graph,
            node_statuses=statuses(),
            build_status=BuildStatus.RUNNING,
            already_scheduled=frozenset({"source.brief"}),
        )
        assert {n.stable_key for n in ready} == {"source.product_reference"}

    def test_cancelling_build_schedules_nothing(self) -> None:
        """§13.4: stop claiming new work as soon as cancellation is requested."""
        graph = orbit_graph()
        assert (
            ready_nodes(graph, node_statuses=statuses(), build_status=BuildStatus.CANCELLING) == ()
        )

    def test_failed_build_schedules_nothing(self) -> None:
        graph = orbit_graph()
        assert ready_nodes(graph, node_statuses=statuses(), build_status=BuildStatus.FAILED) == ()

    def test_ordering_is_deterministic(self) -> None:
        """§12.6 requires deterministic state events, so two identical builds must
        schedule identically."""
        graph = orbit_graph()
        args = {"node_statuses": statuses(), "build_status": BuildStatus.RUNNING}
        assert ready_nodes(graph, **args) == ready_nodes(graph, **args)


class TestCompletion:
    def test_all_passed_succeeds(self) -> None:
        graph = orbit_graph()
        outcome = evaluate_build_completion(
            graph, node_statuses=all_passed_except(), build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.SUCCEEDED

    def test_all_reused_succeeds(self) -> None:
        """A no-op incremental build is still a successful build."""
        graph = orbit_graph()
        reused = {k: BuildNodeStatus.REUSED for k in graph.topological_order}
        outcome = evaluate_build_completion(
            graph, node_statuses=reused, build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.SUCCEEDED

    def test_one_failed_required_node_fails_the_build(self) -> None:
        """The precedence that matters: a terminal failure outranks everything
        else finishing."""
        graph = orbit_graph()
        node_statuses = all_passed_except()
        node_statuses["video.clip.03"] = BuildNodeStatus.FAILED
        outcome = evaluate_build_completion(
            graph, node_statuses=node_statuses, build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.FAILED
        assert outcome.blocking_nodes == ("video.clip.03",)

    def test_waiting_review_prevents_success(self) -> None:
        """§5.4 FR-BUILD-008: "Failed or waiting-review nodes prevent success.""" ""
        graph = orbit_graph()
        node_statuses = all_passed_except()
        node_statuses["video.clip.02"] = BuildNodeStatus.WAITING_REVIEW
        outcome = evaluate_build_completion(
            graph, node_statuses=node_statuses, build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.WAITING_REVIEW
        assert outcome.blocking_nodes == ("video.clip.02",)

    def test_cancelled_required_node_is_not_success(self) -> None:
        """CANCELLED is terminal but not accepted. Treating "all terminal" as
        success would ship a package with a missing clip."""
        graph = orbit_graph()
        node_statuses = all_passed_except()
        node_statuses["video.clip.01"] = BuildNodeStatus.CANCELLED
        outcome = evaluate_build_completion(
            graph, node_statuses=node_statuses, build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.FAILED
        assert "video.clip.01" in outcome.blocking_nodes

    def test_work_remaining_keeps_the_build_running(self) -> None:
        graph = orbit_graph()
        outcome = evaluate_build_completion(
            graph, node_statuses=statuses(), build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.RUNNING

    def test_failure_outranks_pending_review(self) -> None:
        graph = orbit_graph()
        node_statuses = all_passed_except()
        node_statuses["video.clip.01"] = BuildNodeStatus.FAILED
        node_statuses["video.clip.02"] = BuildNodeStatus.WAITING_REVIEW
        outcome = evaluate_build_completion(
            graph, node_statuses=node_statuses, build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.FAILED

    def test_review_blocking_all_remaining_work_reports_waiting(self) -> None:
        """The hang case: nothing runnable, nothing in flight, so the build must
        say a decision is needed rather than sit in RUNNING forever."""
        graph = orbit_graph()
        node_statuses = {k: BuildNodeStatus.PASSED for k in graph.topological_order}
        node_statuses["copy.pack"] = BuildNodeStatus.WAITING_REVIEW
        node_statuses["audio.narration"] = BuildNodeStatus.PENDING
        node_statuses["compose.delivery_package"] = BuildNodeStatus.PENDING

        outcome = evaluate_build_completion(
            graph, node_statuses=node_statuses, build_status=BuildStatus.RUNNING
        )
        assert outcome.status is BuildStatus.WAITING_REVIEW
        assert "copy.pack" in outcome.blocking_nodes

    def test_cancelling_waits_for_in_flight_nodes(self) -> None:
        """§13.4: finalise only once workers acknowledge, so an in-flight provider
        call is not orphaned."""
        graph = orbit_graph()
        node_statuses = statuses(video__clip__01=BuildNodeStatus.RUNNING)
        outcome = evaluate_build_completion(
            graph, node_statuses=node_statuses, build_status=BuildStatus.CANCELLING
        )
        assert outcome.status is BuildStatus.CANCELLING
        assert outcome.blocking_nodes == ("video.clip.01",)

    def test_cancelling_completes_when_nothing_is_in_flight(self) -> None:
        graph = orbit_graph()
        outcome = evaluate_build_completion(
            graph, node_statuses=statuses(), build_status=BuildStatus.CANCELLING
        )
        assert outcome.status is BuildStatus.CANCELLED

    def test_terminal_build_is_left_alone(self) -> None:
        graph = orbit_graph()
        outcome = evaluate_build_completion(
            graph, node_statuses=all_passed_except(), build_status=BuildStatus.SUCCEEDED
        )
        assert outcome.status is BuildStatus.SUCCEEDED
