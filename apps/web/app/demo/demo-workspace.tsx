"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, useTransition } from "react";
import { Icon } from "@/components/icon";
import { IconRail } from "@/components/demo/icon-rail";
import { ImpactPanel } from "@/components/demo/impact-panel";
import { NodeDetail } from "@/components/demo/node-detail";
import { NodeNav } from "@/components/demo/node-nav";
import { SseStatus, type SseState } from "@/components/demo/sse-status";
import { StatusPill } from "@/components/demo/status-pill";
import { StoryboardGrid } from "@/components/demo/storyboard-grid";
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
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="md:hidden text-muted"
              aria-label="Open node list"
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
          <div className="flex flex-wrap items-center gap-3">
            <SseStatus state={sseState} detail={error ?? undefined} />
            <button
              type="button"
              onClick={() => setRailMode("impact")}
              className="inline-flex items-center gap-2 border border-signal/50 bg-signal/10 px-3 py-2 text-xs font-medium uppercase tracking-wide text-signal transition-transform active:scale-95"
            >
              <Icon name="rebuild" className="size-3.5" />
              Edit legal line
            </button>
            {releaseHref ? (
              <Link
                href={releaseHref}
                className="inline-flex items-center gap-2 border border-border px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted hover:text-ink"
              >
                <Icon name="release" className="size-3.5" />
                Release {project.release_version}
              </Link>
            ) : null}
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          <main id="main" className="min-w-0 flex-1 overflow-y-auto p-4 md:p-6">
            <div className="mb-4 flex items-end justify-between gap-4">
              <div>
                <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
                  ORBIT storyboard
                </p>
                <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                  {graph.build.total_nodes} nodes
                </h1>
              </div>
              <p className="font-mono text-[10px] uppercase tracking-wider text-muted">
                {graph.nodes.length} loaded from API
              </p>
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
