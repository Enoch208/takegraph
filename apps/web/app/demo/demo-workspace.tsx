"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, useTransition } from "react";
import { Icon } from "@/components/icon";
import { FeatureCard } from "@/components/demo/feature-card";
import { IconRail } from "@/components/demo/icon-rail";
import { ImpactPanel } from "@/components/demo/impact-panel";
import { MetricRow } from "@/components/demo/metric-row";
import { NodeDetail } from "@/components/demo/node-detail";
import { NodeNav } from "@/components/demo/node-nav";
import { SparkArea, SparkBars } from "@/components/demo/spark";
import { SseStatus, type SseState } from "@/components/demo/sse-status";
import { StatCard, type StatTone } from "@/components/demo/stat-card";
import { StatusPill } from "@/components/demo/status-pill";
import { StoryboardGrid } from "@/components/demo/storyboard-grid";
import { buildMetrics, formatDuration } from "@/lib/build-metrics";
import type { IconName } from "@/components/icon";
import {
  buildEventsUrl,
  commitImpactPlan,
  createChangeSet,
  createDemoSession,
  fetchBuildGraph,
  fetchDemoProject,
  previewImpact,
  requestLiveRetake,
  shortId,
  type BuildGraph,
  type BuildNode,
  type DemoProject,
  type ImpactPlan,
} from "@/lib/api";
import {
  readDemoSession,
  readSseCursor,
  writeDemoSession,
  writeSseCursor,
} from "@/lib/demo-session";
import { consumeBuildEvents } from "@/lib/sse";

type RailMode = "node" | "impact";

function applyEventToNodes(nodes: BuildNode[], payload: Record<string, unknown>): BuildNode[] {
  const stableKey = typeof payload.stable_key === "string" ? payload.stable_key : null;
  if (!stableKey) {
    return nodes;
  }
  return nodes.map((node) => {
    if (node.stable_key !== stableKey) {
      return node;
    }
    const next = { ...node };
    if (typeof payload.to === "string") {
      next.status = payload.to;
    }
    if (typeof payload.activity === "string") {
      next.current_activity = payload.activity;
    }
    if (payload.activity === null) {
      next.current_activity = null;
    }
    if (typeof payload.reason_code === "string") {
      next.reason_code = payload.reason_code;
    }
    if (typeof payload.reason === "string") {
      next.reason = payload.reason;
    }
    return next;
  });
}

export function DemoWorkspace() {
  const [token, setToken] = useState<string | null>(null);
  const [project, setProject] = useState<DemoProject | null>(null);
  const [graph, setGraph] = useState<BuildGraph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [railMode, setRailMode] = useState<RailMode>("node");
  const [draftLine, setDraftLine] = useState("");
  const [plan, setPlan] = useState<ImpactPlan | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [previewing, startPreview] = useTransition();
  const [committing, startCommit] = useTransition();
  const [sseState, setSseState] = useState<SseState>("idle");
  const [liveBusy, setLiveBusy] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setError(null);
      let session = readDemoSession();
      if (!session || session.expires_at * 1000 < Date.now() + 30_000) {
        const issued = await createDemoSession();
        if (!issued.ok) {
          if (!cancelled) {
            setError(issued.error);
          }
          return;
        }
        session = issued.data;
        writeDemoSession(session);
      }
      if (cancelled) {
        return;
      }
      setToken(session.access_token);
      const demoProject = await fetchDemoProject(session.access_token);
      if (!demoProject.ok) {
        if (!cancelled) {
          setError(demoProject.error);
        }
        return;
      }
      if (cancelled) {
        return;
      }
      setProject(demoProject.data);
      setDraftLine(demoProject.data.legal_line.replace("zero sugar", "no added sugar"));
      const buildGraph = await fetchBuildGraph(demoProject.data.build_id, session.access_token);
      if (!buildGraph.ok) {
        if (!cancelled) {
          setError(buildGraph.error);
        }
        return;
      }
      if (!cancelled) {
        setGraph(buildGraph.data);
        setSelectedKey(buildGraph.data.nodes[0]?.stable_key ?? null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!token || !graph) {
      return;
    }
    const buildId = graph.build.id;
    const controller = new AbortController();
    let cursor = Math.max(readSseCursor(buildId), graph.latest_event_sequence);
    setSseState("connecting");

    const run = async () => {
      try {
        await consumeBuildEvents({
          url: buildEventsUrl(buildId),
          token,
          cursor,
          signal: controller.signal,
          onHeartbeat: () => setSseState("live"),
          onMessage: (message) => {
            setSseState("live");
            cursor = message.id;
            writeSseCursor(buildId, message.id);
            const envelope = message.data as {
              event_type?: string;
              payload?: Record<string, unknown>;
              build_id?: string;
            };
            const payload = envelope.payload ?? {};
            if (
              message.event === "build.node.status_changed" ||
              message.event === "build.node.activity_changed" ||
              envelope.event_type === "build.node.status_changed" ||
              envelope.event_type === "build.node.activity_changed"
            ) {
              setGraph((current) =>
                current
                  ? {
                      ...current,
                      latest_event_sequence: message.id,
                      nodes: applyEventToNodes(current.nodes, payload),
                    }
                  : current,
              );
            }
            if (
              message.event === "build.status_changed" ||
              envelope.event_type === "build.status_changed"
            ) {
              const to = typeof payload.to === "string" ? payload.to : null;
              if (to) {
                setGraph((current) =>
                  current
                    ? {
                        ...current,
                        build: { ...current.build, status: to },
                        latest_event_sequence: message.id,
                      }
                    : current,
                );
              }
              void fetchBuildGraph(buildId, token).then((fresh) => {
                if (fresh.ok) {
                  setGraph(fresh.data);
                }
              });
            }
          },
        });
        setSseState("idle");
      } catch (cause) {
        if (controller.signal.aborted) {
          return;
        }
        setSseState("error");
        setError(cause instanceof Error ? cause.message : "SSE connection failed.");
      }
    };
    void run();
    return () => controller.abort();
  }, [token, graph?.build.id]);

  const selectedNode = useMemo(
    () => graph?.nodes.find((node) => node.stable_key === selectedKey) ?? null,
    [graph, selectedKey],
  );

  const counts = useMemo(() => {
    const nodes = graph?.nodes ?? [];
    return {
      reused: graph?.build.reused_nodes ?? 0,
      rebuilt: graph?.build.rebuilt_nodes ?? 0,
      running: nodes.filter((node) =>
        ["RUNNING", "QUEUED", "FALLBACK_PENDING", "RETAKE_PENDING"].includes(node.status),
      ).length,
      failed: nodes.filter((node) => ["FAILED", "BLOCKED"].includes(node.status)).length,
    };
  }, [graph]);

  const impactByKey = useMemo(() => {
    if (!plan) {
      return undefined;
    }
    return new Map(plan.nodes.map((node) => [node.stable_key, node.decision]));
  }, [plan]);

  const releaseHref = project ? `/releases/${project.release_id}` : undefined;

  const onPreview = () => {
    if (!token || !project) {
      return;
    }
    setImpactError(null);
    startPreview(async () => {
      const changeSet = await createChangeSet(project.project_id, token, {
        base_revision_id: project.active_revision_id,
        patch: { parameters: { legal_line: draftLine.trim() } },
      });
      if (!changeSet.ok) {
        setImpactError(changeSet.error);
        return;
      }
      const impact = await previewImpact(changeSet.data.id, token);
      if (!impact.ok) {
        setImpactError(impact.error);
        return;
      }
      setPlan(impact.data);
    });
  };

  const onCommit = () => {
    if (!token || !plan) {
      return;
    }
    setImpactError(null);
    startCommit(async () => {
      const committed = await commitImpactPlan(
        plan.plan_id,
        token,
        plan.plan_hash,
        crypto.randomUUID(),
      );
      if (!committed.ok) {
        setImpactError(committed.error);
        return;
      }
      const fresh = await fetchBuildGraph(committed.data.build_id, token);
      if (!fresh.ok) {
        setImpactError(fresh.error);
        return;
      }
      setGraph(fresh.data);
      setProject((current) =>
        current
          ? {
              ...current,
              build_id: committed.data.build_id,
              build_status: committed.data.status,
              active_revision_id: committed.data.project_revision_id,
              legal_line: draftLine.trim(),
            }
          : current,
      );
      setRailMode("node");
    });
  };

  const onLiveRetake = () => {
    if (!token) {
      return;
    }
    setLiveBusy(true);
    setLiveError(null);
    void (async () => {
      const result = await requestLiveRetake(token, crypto.randomUUID());
      setLiveBusy(false);
      if (!result.ok) {
        setLiveError(result.error);
        return;
      }
      const fresh = await fetchBuildGraph(result.data.build_id, token);
      if (!fresh.ok) {
        setLiveError(fresh.error);
        return;
      }
      setGraph(fresh.data);
      setSelectedKey(result.data.stable_key);
      setProject((current) =>
        current
          ? { ...current, build_id: result.data.build_id, build_status: "QUEUED" }
          : current,
      );
    })();
  };

  if (error && !graph) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas px-6">
        <div className="max-w-lg border border-danger/40 bg-surface p-8">
          <p className="font-mono text-[10px] uppercase tracking-wider text-danger">
            Demo unavailable
          </p>
          <p className="mt-3 text-sm text-muted">{error}</p>
          <Link href="/" className="mt-6 inline-flex text-xs uppercase tracking-wide text-signal">
            Back to landing
          </Link>
        </div>
      </div>
    );
  }

  if (!token || !project || !graph) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas">
        <p className="font-mono text-xs uppercase tracking-wider text-muted">Loading ORBIT…</p>
      </div>
    );
  }

  const isLiveBuild = Boolean(graph.build.parent_build_id);

  // Derived after the guards above so `graph` is known present. Eighteen nodes is
  // not worth memoising, and a stale memo on a live-streaming view is a worse
  // problem than the arithmetic.
  const metrics = buildMetrics(graph);

  // The fourth KPI is whichever state most needs a person's attention. A card
  // that always reads "0 failed" spends a quarter of the strip saying nothing;
  // this one escalates failure over review over completion.
  const attention: {
    icon: IconName;
    label: string;
    value: number | string;
    detail: string;
    tone: StatTone;
  } =
    metrics.failed > 0
      ? {
          icon: "failed",
          label: "Failed nodes",
          value: metrics.failed,
          detail: "build cannot release",
          tone: "danger",
        }
      : metrics.review > 0
        ? {
            icon: "review",
            label: "Awaiting review",
            value: metrics.review,
            detail: "needs a decision",
            tone: "review",
          }
        : {
            icon: "running",
            label: "Build duration",
            value: formatDuration(metrics.durationSeconds),
            detail: graph.build.status.toLowerCase(),
            tone: metrics.running > 0 ? "active" : "verified",
          };

  return (
    <div className="flex h-svh overflow-hidden bg-canvas text-ink">
      <IconRail releaseHref={releaseHref} />

      <div
        className={`${navOpen ? "fixed inset-y-0 left-14 z-40 flex" : "hidden"} md:static md:flex`}
      >
        <NodeNav
          projectName={project.name}
          build={graph.build}
          nodes={graph.nodes}
          selectedKey={selectedKey}
          onSelect={(key) => {
            setSelectedKey(key);
            setRailMode("node");
            setNavOpen(false);
          }}
          counts={counts}
        />
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline bg-surface px-4 py-2.5">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="hit flex items-center justify-center rounded-[var(--radius-control)] text-muted hover:bg-elevated hover:text-ink md:hidden"
              aria-label="Open node list"
              aria-expanded={navOpen}
              onClick={() => setNavOpen((value) => !value)}
            >
              <Icon name="menu" className="size-5" />
            </button>
            <div>
              <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
                Live build
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold tracking-tight">
                  {project.slug} · {shortId(graph.build.id)}
                </span>
                <StatusPill status={graph.build.status} />
                {isLiveBuild ? <StatusPill status="LIVE" label="LIVE" /> : null}
                {!graph.build.is_fixture ? (
                  <StatusPill status="PASSED" label="REAL BASELINE" />
                ) : (
                  <StatusPill status="TEST_FAULT" label="REPLAY OF REAL RUN" />
                )}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <SseStatus
              state={sseState}
              buildStatus={graph.build.status}
              detail={error ?? undefined}
            />
            <button
              type="button"
              onClick={() => setRailMode("impact")}
              className="inline-flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] bg-signal px-3.5 text-xs font-semibold text-canvas transition-[transform,background-color] hover:bg-signal/90 active:scale-[0.98]"
            >
              <Icon name="rebuild" className="size-4" />
              Edit legal line
            </button>
            {releaseHref ? (
              <Link
                href={releaseHref}
                className="inline-flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] border border-hairline px-3.5 text-xs font-medium text-muted transition-colors hover:bg-elevated hover:text-ink"
              >
                <Icon name="release" className="size-4" />
                Release {project.release_version}
              </Link>
            ) : null}
          </div>
        </header>

        {/* Screen readers get the same live narration the status dot gives sighted
            users. Polite, not assertive — a build event must never interrupt
            someone mid-sentence. §18.14. */}
        <p aria-live="polite" className="sr-only">
          {`Build ${graph.build.status.toLowerCase()}. ${metrics.complete} of ${metrics.total} nodes complete, ${metrics.reused} reused, ${metrics.rebuilt} rebuilt.`}
        </p>

        <div className="flex min-h-0 flex-1">
          <main id="main" className="min-w-0 flex-1 overflow-y-auto p-4 md:p-5">
            {/* KPI strip. Four figures, the shape the reference opens with, mapped
                to what a build actually has: how big it is, what it skipped, what
                it did, and whether anything needs a person. */}
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
              <StatCard
                icon="layers"
                label="Nodes in graph"
                value={metrics.total}
                detail={`${metrics.complete} complete`}
              />
              <StatCard
                icon="reused"
                label="Reused from cache"
                value={metrics.reused}
                detail={`${metrics.reusePct}% of graph`}
                tone="verified"
              />
              <StatCard
                icon="rebuild"
                label="Rebuilt this run"
                value={metrics.rebuilt}
                detail={metrics.running > 0 ? `${metrics.running} in flight` : "none in flight"}
                tone="signal"
              />
              <StatCard
                icon={attention.icon}
                label={attention.label}
                value={attention.value}
                detail={attention.detail}
                tone={attention.tone}
              />
            </div>

            {/* The dominant card, where the reference puts its bar chart. The
                storyboard is this product's chart — it is the artifact and the
                status display at once. */}
            <section className="panel mt-3 p-4" aria-labelledby="storyboard-heading">
              <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
                <div>
                  <h1
                    id="storyboard-heading"
                    className="text-[15px] font-semibold tracking-tight text-ink"
                  >
                    {project.name} storyboard
                  </h1>
                  <p className="mt-0.5 text-xs text-muted">
                    Every node in the graph, in build order. Select one to inspect its evidence.
                  </p>
                </div>
                <div className="flex items-center gap-3 font-mono text-[10px] uppercase tracking-wider">
                  <span className="flex items-center gap-1.5 text-signal">
                    <span className="size-1.5 rounded-full bg-signal" />
                    Rebuilt
                  </span>
                  <span className="flex items-center gap-1.5 text-faint">
                    <span className="size-1.5 rounded-full bg-muted/50" />
                    Reused
                  </span>
                </div>
              </div>
              <StoryboardGrid
                nodes={graph.nodes}
                selectedKey={selectedKey}
                token={token}
                onSelect={(key) => {
                  setSelectedKey(key);
                  setRailMode("node");
                }}
                impactByKey={impactByKey}
              />
            </section>

            {/* The reference's bottom row: a metrics card beside two accent cards. */}
            <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
              <section className="panel p-4" aria-labelledby="metrics-heading">
                <h2 id="metrics-heading" className="text-[13px] font-semibold tracking-tight">
                  Build metrics
                </h2>
                <div className="mt-4 space-y-3">
                  <MetricRow
                    label="Complete"
                    value={metrics.complete}
                    total={metrics.total}
                    tone="verified"
                  />
                  <MetricRow
                    label="Reused"
                    value={metrics.reused}
                    total={metrics.total}
                    tone="verified"
                  />
                  <MetricRow
                    label="Rebuilt"
                    value={metrics.rebuilt}
                    total={metrics.total}
                    tone="signal"
                  />
                  <MetricRow
                    label="Quality gates"
                    value={metrics.gatesPassed}
                    total={metrics.gatesTotal}
                    tone="active"
                  />
                </div>
              </section>

              <div className="grid gap-3 sm:grid-cols-2 lg:w-[26rem]">
                <FeatureCard
                  icon="verified"
                  label="Progress"
                  value={`${metrics.progressPct}%`}
                  caption={`${metrics.complete} of ${metrics.total} nodes · ${formatDuration(metrics.durationSeconds)}`}
                  accent="verified"
                >
                  <SparkArea
                    series={metrics.completionSeries}
                    className="h-full w-full"
                    title={`Cumulative nodes completed: ${metrics.complete} of ${metrics.total}`}
                  />
                </FeatureCard>

                <FeatureCard
                  icon="recover"
                  label="Self-healed"
                  value={String(metrics.recovered)}
                  caption={
                    metrics.injectedFaults > 0
                      ? `${metrics.attempts} attempts · ${metrics.injectedFaults} test fault`
                      : `${metrics.attempts} attempts across ${metrics.total} nodes`
                  }
                  accent="signal"
                >
                  <SparkBars bars={metrics.attemptsPerNode} className="h-full w-full" />
                </FeatureCard>
              </div>
            </div>
          </main>

          <div className="hidden lg:flex">
            {railMode === "impact" ? (
              <ImpactPanel
                legalLine={project.legal_line}
                draftLine={draftLine}
                onDraftChange={setDraftLine}
                onPreview={onPreview}
                onCommit={onCommit}
                onClose={() => setRailMode("node")}
                previewing={previewing}
                committing={committing}
                plan={plan}
                error={impactError}
              />
            ) : selectedNode ? (
              <NodeDetail
                node={selectedNode}
                token={token}
                onLiveRetake={onLiveRetake}
                liveBusy={liveBusy}
                liveError={liveError}
                isLiveBuild={isLiveBuild && selectedNode.stable_key === "video.clip.03"}
              />
            ) : null}
          </div>
        </div>

        <div className="border-t border-border lg:hidden">
          {railMode === "impact" ? (
            <ImpactPanel
              legalLine={project.legal_line}
              draftLine={draftLine}
              onDraftChange={setDraftLine}
              onPreview={onPreview}
              onCommit={onCommit}
              onClose={() => setRailMode("node")}
              previewing={previewing}
              committing={committing}
              plan={plan}
              error={impactError}
            />
          ) : selectedNode ? (
            <div className="max-h-[45vh] overflow-hidden">
              <NodeDetail
                node={selectedNode}
                token={token}
                onLiveRetake={onLiveRetake}
                liveBusy={liveBusy}
                liveError={liveError}
                isLiveBuild={isLiveBuild && selectedNode.stable_key === "video.clip.03"}
              />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
