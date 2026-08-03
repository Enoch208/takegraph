"use client";

import type { BuildNode } from "@/lib/api";
import { MediaThumb } from "@/components/demo/media-thumb";
import { StatusPill } from "@/components/demo/status-pill";

function previewAsset(node: BuildNode) {
  const selected = node.selected_assets[0];
  if (selected) {
    return selected;
  }
  for (const attempt of node.attempts) {
    const asset = attempt.assets[0];
    if (asset) {
      return asset;
    }
  }
  return null;
}

export function StoryboardGrid({
  nodes,
  selectedKey,
  token,
  onSelect,
  impactByKey,
}: {
  nodes: BuildNode[];
  selectedKey: string | null;
  token: string;
  onSelect: (stableKey: string) => void;
  impactByKey?: Map<string, string>;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
      {nodes.map((node) => {
        const selected = node.stable_key === selectedKey;
        const fault = node.attempts.some((a) => a.is_injected_fault);
        const impact = impactByKey?.get(node.stable_key);
        const asset = previewAsset(node);
        return (
          <button
            key={node.id}
            type="button"
            onClick={() => onSelect(node.stable_key)}
            className={`group corner-ticks flex flex-col border bg-surface text-left transition-colors ${
              selected
                ? "border-active"
                : impact === "REBUILD"
                  ? "border-signal/60"
                  : "border-dashed border-border hover:border-ink/30"
            }`}
          >
            <div className="relative aspect-video w-full overflow-hidden border-b border-border bg-canvas">
              <MediaThumb asset={asset} token={token} className="h-full w-full" />
              {node.current_activity ? (
                <div className="absolute inset-x-0 bottom-0 bg-canvas/80 px-2 py-1 font-mono text-[9px] uppercase tracking-wider text-active">
                  {node.current_activity}
                </div>
              ) : null}
            </div>
            <div className="flex flex-1 flex-col gap-2 p-3">
              <div className="flex items-start justify-between gap-2">
                <p className="text-xs font-semibold tracking-tight text-ink">{node.label}</p>
                <StatusPill status={node.status} />
              </div>
              <p className="font-mono text-[10px] text-muted">{node.stable_key}</p>
              <div className="mt-auto flex flex-wrap gap-1">
                {fault ? <StatusPill status="TEST_FAULT" label="TEST FAULT" /> : null}
                {impact ? <StatusPill status={impact} /> : null}
                {node.resolution === "EXACT_VALIDATED_REUSE" ? (
                  <StatusPill status="REUSED" label="REUSE" />
                ) : null}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
