"""Scheduling and completion rules (PRD §12.6, §5.4 FR-BUILD-008).

Pure functions over node state. No database, no clock, no queue — which means the
rules that decide whether a build succeeds can be tested exhaustively, and the
worker's persistence concerns cannot quietly change them.

The two questions this module answers:

1. Which nodes may start right now?
2. Has the build finished, and if so how?

Both are asked after every terminal node transition, so both must be cheap and
must never depend on the order nodes happen to be stored in.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from takegraph_domain.enums import BuildNodeStatus, BuildStatus, NodeType
from takegraph_domain.graph.types import CompiledGraph

#: §12.6: "prioritize short/control nodes ahead of expensive media nodes only when
#: dependencies allow". Higher runs first. Cheap text and planning work unblocks
#: downstream media, so running it first shortens the critical path; doing the
#: reverse leaves a video node hogging a slot while a one-second copy node waits.
NODE_TYPE_PRIORITY: dict[NodeType, int] = {
    NodeType.SOURCE_TEXT: 100,
    NodeType.SOURCE_IMAGE: 100,
    NodeType.STRUCTURED_PLAN: 90,
    NodeType.STRUCTURED_TEXT: 90,
    NodeType.IMAGE_TRANSFORM: 80,
    NodeType.IMAGE_COMPOSITION: 70,
    NodeType.AUDIO_GENERATION: 60,
    NodeType.IMAGE_GENERATION: 50,
    NodeType.VIDEO_GENERATION: 30,
    NodeType.MEDIA_COMPOSITION: 20,
}


def node_priority(node_type: NodeType) -> int:
    return NODE_TYPE_PRIORITY.get(node_type, 50)


class ReadyNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_key: str
    node_type: NodeType
    priority: int


def ready_nodes(
    graph: CompiledGraph,
    *,
    node_statuses: Mapping[str, BuildNodeStatus],
    build_status: BuildStatus,
    already_scheduled: frozenset[str] = frozenset(),
) -> tuple[ReadyNode, ...]:
    """Nodes eligible to start, highest priority first.

    §12.6 requires all of: every required predecessor PASSED or REUSED, the build
    runnable, and no active work item already existing for the node.

    Cancellation stops scheduling immediately — §13.4 says stop claiming new work
    for the build, so returning candidates during CANCELLING would race the
    cancellation itself.
    """
    if build_status not in (BuildStatus.RUNNING, BuildStatus.QUEUED):
        return ()

    by_key = graph.by_key
    candidates: list[ReadyNode] = []

    for stable_key in graph.topological_order:
        if stable_key in already_scheduled:
            continue
        if node_statuses.get(stable_key, BuildNodeStatus.PENDING) is not BuildNodeStatus.PENDING:
            continue

        node = by_key[stable_key]
        dependencies_met = all(
            node_statuses.get(slot.from_key, BuildNodeStatus.PENDING).satisfies_dependency
            for slot in node.inputs
        )
        if dependencies_met:
            candidates.append(
                ReadyNode(
                    stable_key=stable_key,
                    node_type=node.node_type,
                    priority=node_priority(node.node_type),
                )
            )

    # Stable ordering: priority first, then topological position. §12.6 requires
    # deterministic state events, so two identical builds must schedule alike.
    position = {key: i for i, key in enumerate(graph.topological_order)}
    return tuple(sorted(candidates, key=lambda n: (-n.priority, position[n.stable_key])))


class BuildOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BuildStatus
    reason: str
    blocking_nodes: tuple[str, ...] = ()


def evaluate_build_completion(
    graph: CompiledGraph,
    *,
    node_statuses: Mapping[str, BuildNodeStatus],
    build_status: BuildStatus,
) -> BuildOutcome:
    """Decide whether the build is finished, and how (§5.4 FR-BUILD-008).

    "Build success requires all required nodes passed/reused/approved. Failed or
    waiting-review nodes prevent success." The ordering below encodes a
    precedence: a terminal failure outranks pending review, which outranks
    success. Checking success first would let a build with one failed required
    node report SUCCEEDED because everything else finished.
    """
    if build_status.is_terminal:
        return BuildOutcome(status=build_status, reason="Build already reached a terminal state.")

    if build_status is BuildStatus.CANCELLING:
        still_running = tuple(
            key
            for key, status in node_statuses.items()
            if status
            in (
                BuildNodeStatus.RUNNING,
                BuildNodeStatus.STORING,
                BuildNodeStatus.VERIFYING,
            )
        )
        if still_running:
            return BuildOutcome(
                status=BuildStatus.CANCELLING,
                reason="Waiting for in-flight nodes to acknowledge cancellation.",
                blocking_nodes=tuple(sorted(still_running)),
            )
        return BuildOutcome(status=BuildStatus.CANCELLED, reason="All work stopped.")

    by_key = graph.by_key
    required = [n.stable_key for n in graph.nodes if n.required]

    failed = tuple(sorted(k for k in required if node_statuses.get(k) is BuildNodeStatus.FAILED))
    if failed:
        return BuildOutcome(
            status=BuildStatus.FAILED,
            reason="A required node failed terminally.",
            blocking_nodes=failed,
        )

    waiting = tuple(
        sorted(k for k in required if node_statuses.get(k) is BuildNodeStatus.WAITING_REVIEW)
    )

    unfinished = tuple(
        sorted(
            k
            for k in required
            if not node_statuses.get(k, BuildNodeStatus.PENDING).is_terminal
            and node_statuses.get(k) is not BuildNodeStatus.WAITING_REVIEW
        )
    )

    if unfinished:
        # Work remains. If nothing can move, the build is stuck behind review
        # rather than progressing — surfacing that is what stops a silent hang.
        runnable = ready_nodes(graph, node_statuses=node_statuses, build_status=BuildStatus.RUNNING)
        in_flight = any(
            status
            in (
                BuildNodeStatus.QUEUED,
                BuildNodeStatus.RUNNING,
                BuildNodeStatus.STORING,
                BuildNodeStatus.VERIFYING,
                BuildNodeStatus.RETRY_PENDING,
                BuildNodeStatus.FALLBACK_PENDING,
                BuildNodeStatus.RETAKE_PENDING,
            )
            for status in node_statuses.values()
        )
        if not runnable and not in_flight and waiting:
            return BuildOutcome(
                status=BuildStatus.WAITING_REVIEW,
                reason="No runnable work remains; a human decision is required.",
                blocking_nodes=waiting,
            )
        return BuildOutcome(
            status=BuildStatus.RUNNING,
            reason="Work remains.",
            blocking_nodes=unfinished,
        )

    if waiting:
        return BuildOutcome(
            status=BuildStatus.WAITING_REVIEW,
            reason="No runnable work remains; a human decision is required.",
            blocking_nodes=waiting,
        )

    # Every required node is terminal and none failed. Confirm each is genuinely
    # accepted rather than merely terminal — CANCELLED is terminal too, and a
    # cancelled required node must not read as success.
    unaccepted = tuple(
        sorted(
            k
            for k in required
            if not node_statuses.get(k, BuildNodeStatus.PENDING).satisfies_dependency
        )
    )
    if unaccepted:
        return BuildOutcome(
            status=BuildStatus.FAILED,
            reason="A required node ended without an accepted output.",
            blocking_nodes=unaccepted,
        )

    _ = by_key  # graph is validated at compile time; retained for future checks
    return BuildOutcome(
        status=BuildStatus.SUCCEEDED,
        reason="Every required node is passed, reused or approved.",
    )
