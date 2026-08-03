"use client";

import type { ImpactPlan } from "@/lib/api";
import { Icon } from "@/components/icon";
import { StatusPill } from "@/components/demo/status-pill";

export function ImpactPanel({
  legalLine,
  draftLine,
  onDraftChange,
  onPreview,
  onCommit,
  onClose,
  previewing,
  committing,
  plan,
  error,
}: {
  legalLine: string;
  draftLine: string;
  onDraftChange: (value: string) => void;
  onPreview: () => void;
  onCommit: () => void;
  onClose: () => void;
  previewing: boolean;
  committing: boolean;
  plan: ImpactPlan | null;
  error: string | null;
}) {
  const rebuildNodes = plan?.nodes.filter((node) => node.decision === "REBUILD") ?? [];

  return (
    <aside className="flex w-full max-w-md shrink-0 flex-col border-l border-border bg-surface">
      <div className="flex items-start justify-between border-b border-border px-4 py-4">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
            Incremental impact
          </p>
          <h3 className="mt-1 text-lg font-semibold tracking-tight text-ink">Legal line</h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-muted hover:text-ink"
          aria-label="Close impact panel"
        >
          <Icon name="close" className="size-4" />
        </button>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4">
        <section>
          <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Current</p>
          <p className="mt-1 text-sm text-muted">{legalLine}</p>
        </section>

        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-wider text-faint">
            Proposed
          </span>
          <input
            value={draftLine}
            onChange={(event) => onDraftChange(event.target.value)}
            className="mt-2 w-full border border-border bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-active"
          />
        </label>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onPreview}
            disabled={previewing || !draftLine.trim() || draftLine === legalLine}
            className="inline-flex items-center gap-2 border border-ink/20 bg-elevated px-3 py-2 text-xs font-medium uppercase tracking-wide text-ink transition-transform active:scale-95 disabled:opacity-40"
          >
            <Icon name="view" className="size-3.5" />
            {previewing ? "Computing…" : "Preview impact"}
          </button>
          <button
            type="button"
            onClick={onCommit}
            disabled={!plan || committing}
            className="inline-flex items-center gap-2 border border-signal bg-signal/15 px-3 py-2 text-xs font-medium uppercase tracking-wide text-signal transition-transform active:scale-95 disabled:opacity-40"
          >
            <Icon name="play" className="size-3.5" />
            {committing ? "Committing…" : "Confirm incremental build"}
          </button>
        </div>

        {error ? (
          <p className="border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        ) : null}

        {plan ? (
          <section className="space-y-4">
            <div className="grid grid-cols-2 gap-2 border border-dashed border-border p-3 font-mono text-[11px] uppercase tracking-wider">
              <div>
                <p className="text-faint">Reuse</p>
                <p className="text-2xl text-verified">{plan.summary.reuse}</p>
              </div>
              <div>
                <p className="text-faint">Rebuild</p>
                <p className="text-2xl text-signal">{plan.summary.rebuild}</p>
              </div>
              <div>
                <p className="text-faint">Provider calls</p>
                <p className="text-ink">{plan.summary.provider_calls}</p>
              </div>
              <div>
                <p className="text-faint">Pricing</p>
                <p className="text-review">
                  {plan.summary.pricing_status === "UNKNOWN"
                    ? "UNKNOWN"
                    : plan.summary.estimated_cost_usd ?? plan.summary.pricing_status}
                </p>
              </div>
            </div>

            <div>
              <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-faint">
                Rebuild nodes
              </p>
              <ul className="space-y-2">
                {rebuildNodes.map((node) => (
                  <li key={node.stable_key} className="border border-border bg-elevated px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-ink">{node.stable_key}</span>
                      <StatusPill status="REBUILD" />
                    </div>
                    <p className="mt-1 font-mono text-[10px] text-signal">{node.reason_code}</p>
                    <p className="mt-1 text-sm text-muted">{node.reason}</p>
                  </li>
                ))}
              </ul>
            </div>

            <p className="break-all font-mono text-[10px] text-faint">
              plan_hash {plan.plan_hash}
            </p>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
