/**
 * Everything the dashboard displays, derived from the build graph the API
 * actually returned.
 *
 * The rule this file exists to enforce: no figure on screen is invented, and no
 * chart is drawn from a shape that looks plausible. Every series here is built
 * from real timestamps and real attempt rows, and when the data to draw one is
 * absent the function returns an empty series so the component can render an
 * honest empty state instead of a decorative curve.
 */

import type { BuildGraph, BuildNode } from "@/lib/api";

/** Statuses that mean the node is finished and satisfied its dependents. */
const COMPLETE = new Set(["PASSED", "REUSED"]);
const RUNNING = new Set([
  "QUEUED",
  "RUNNING",
  "STORING",
  "VERIFYING",
  "RETRY_PENDING",
  "FALLBACK_PENDING",
  "RETAKE_PENDING",
]);
const REVIEW = new Set(["WAITING_REVIEW"]);
const FAILED = new Set(["FAILED", "BLOCKED", "CANCELLED"]);

export type Bucket = { at: number; value: number };

export type BuildMetrics = {
  total: number;
  reused: number;
  rebuilt: number;
  complete: number;
  running: number;
  review: number;
  failed: number;
  /** Percentage of nodes finished, 0–100. */
  progressPct: number;
  /** Percentage of nodes served from cache, 0–100. */
  reusePct: number;
  attempts: number;
  /** Nodes that failed at least once and still finished — self-healing, observed. */
  recovered: number;
  /** Attempts the demo's fault injector produced (§8.3.11), labelled TEST FAULT. */
  injectedFaults: number;
  gatesPassed: number;
  gatesTotal: number;
  /** Wall-clock seconds, or null while the build has not started. */
  durationSeconds: number | null;
  /** Cumulative nodes complete over the build's wall clock. Empty until it starts. */
  completionSeries: Bucket[];
  /** Attempt count per node in graph order — a bar above 1 is a recovery. */
  attemptsPerNode: { key: string; label: string; attempts: number; healed: boolean }[];
};

function time(value: string | null): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Cumulative completions bucketed across the build's wall clock.
 *
 * Nodes that finished without a `completed_at` — a reuse resolved at plan time —
 * are counted at the build's start, which is where they became available.
 */
function completionSeries(graph: BuildGraph, buckets = 28): Bucket[] {
  const start = time(graph.build.started_at) ?? time(graph.build.created_at);
  if (start === null) return [];
  const end = time(graph.build.completed_at) ?? Date.now();
  const span = Math.max(end - start, 1);

  const stamps: number[] = [];
  for (const node of graph.nodes) {
    if (!COMPLETE.has(node.status)) continue;
    stamps.push(time(node.completed_at) ?? start);
  }
  if (stamps.length === 0) return [];
  stamps.sort((a, b) => a - b);

  const series: Bucket[] = [];
  for (let index = 0; index <= buckets; index += 1) {
    const at = start + (span * index) / buckets;
    let count = 0;
    for (const stamp of stamps) {
      if (stamp > at) break;
      count += 1;
    }
    series.push({ at, value: count });
  }
  return series;
}

function attemptsPerNode(nodes: BuildNode[]) {
  return nodes.map((node) => ({
    key: node.stable_key,
    label: node.label,
    attempts: node.attempts.length,
    healed: node.attempts.length > 1 && COMPLETE.has(node.status),
  }));
}

export function buildMetrics(graph: BuildGraph): BuildMetrics {
  const nodes = graph.nodes;
  const total = graph.build.total_nodes || nodes.length;

  let complete = 0;
  let running = 0;
  let review = 0;
  let failed = 0;
  let attempts = 0;
  let recovered = 0;
  let injectedFaults = 0;
  let gatesPassed = 0;
  let gatesTotal = 0;

  for (const node of nodes) {
    if (COMPLETE.has(node.status)) complete += 1;
    else if (RUNNING.has(node.status)) running += 1;
    else if (REVIEW.has(node.status)) review += 1;
    else if (FAILED.has(node.status)) failed += 1;

    attempts += node.attempts.length;
    if (node.attempts.length > 1 && COMPLETE.has(node.status)) recovered += 1;
    for (const attempt of node.attempts) {
      if (attempt.is_injected_fault) injectedFaults += 1;
    }
    for (const validation of node.validations) {
      gatesTotal += 1;
      if (validation.status === "PASSED") gatesPassed += 1;
    }
  }

  const started = time(graph.build.started_at);
  const finished = time(graph.build.completed_at);

  return {
    total,
    reused: graph.build.reused_nodes,
    rebuilt: graph.build.rebuilt_nodes,
    complete,
    running,
    review,
    failed,
    progressPct: total > 0 ? Math.round((complete / total) * 100) : 0,
    reusePct: total > 0 ? Math.round((graph.build.reused_nodes / total) * 100) : 0,
    attempts,
    recovered,
    injectedFaults,
    gatesPassed,
    gatesTotal,
    durationSeconds:
      started === null ? null : Math.max(0, Math.round(((finished ?? Date.now()) - started) / 1000)),
    completionSeries: completionSeries(graph),
    attemptsPerNode: attemptsPerNode(nodes),
  };
}

/** `13m 01s`, or an em dash when the build has not started. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${rest}s`;
  return `${minutes}m ${String(rest).padStart(2, "0")}s`;
}
