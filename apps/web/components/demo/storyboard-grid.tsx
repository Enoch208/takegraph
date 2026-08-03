"use client";

import type { BuildNode } from "@/lib/api";
import { MediaThumb } from "@/components/demo/media-thumb";
import { StatusPill } from "@/components/demo/status-pill";
import { inStoryOrder, sectionFor, STORY_SECTIONS } from "@/components/demo/storyboard-order";

/** Best asset to show on a card.
 *
 * An image is preferred wherever one exists: a still paints immediately, while a
 * video element has to fetch and decode a frame before it shows anything but
 * black. The delivery package carries real thumbnails, so it gets one; the clips
 * only have their primary video, so they fall back to it.
 */
function previewAsset(node: BuildNode) {
  const candidates = [...node.selected_assets, ...node.attempts.flatMap((a) => a.assets)];
  return (
    candidates.find((a) => a.media_kind === "IMAGE") ??
    candidates.find((a) => a.media_kind === "VIDEO") ??
    candidates[0] ??
    null
  );
}

/**
 * Storyboard (PRD §18.6, §18.7).
 *
 * The whole product claim is "these few changed, these many did not", and a grid
 * that renders both with equal weight throws that away. So rank is explicit:
 *
 *   rebuilt  — full opacity, signal border, live corner ticks. The only orange.
 *   reused   — dimmed, hairline, quiet. Present as evidence, not as news.
 *
 * §18.14 requires every status colour to carry a text or icon equivalent, so the
 * chips stay regardless of the styling above; the dimming is emphasis, never the
 * only signal.
 */
/** Terminal node states, where the activity strip would only echo the pill. */
const TERMINAL_STATUSES = new Set(["PASSED", "REUSED", "FAILED", "CANCELLED"]);

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
  const ordered = inStoryOrder(nodes);

  return (
    <div className="space-y-8">
      {STORY_SECTIONS.map((section) => {
        const members = ordered.filter((n) => sectionFor(n.stable_key) === section.title);
        if (members.length === 0) return null;

        return (
          <section key={section.title}>
            <div className="mb-3 flex items-center gap-3">
              <h3 className="font-mono text-[10px] uppercase tracking-widest text-faint">
                {section.title}
              </h3>
              <span className="h-px flex-1 bg-border" />
              <span className="font-mono text-[10px] text-faint">{members.length}</span>
            </div>

            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
              {members.map((node) => {
                const selected = node.stable_key === selectedKey;
                const fault = node.attempts.some((a) => a.is_injected_fault);
                const impact = impactByKey?.get(node.stable_key);
                const asset = previewAsset(node);

                // Rebuilt in this build, or marked for rebuild by a pending plan.
                const isRebuilt = impact === "REBUILD" || node.status === "PASSED";
                const isReused = node.status === "REUSED";

                return (
                  <button
                    key={node.id}
                    type="button"
                    onClick={() => onSelect(node.stable_key)}
                    aria-current={selected ? "true" : undefined}
                    className={`group flex flex-col border text-left transition-all duration-300 ${
                      isRebuilt ? "corner-ticks" : ""
                    } ${
                      selected
                        ? "border-active bg-surface opacity-100"
                        : isRebuilt
                          ? "border-signal/50 bg-surface opacity-100 hover:border-signal"
                          : // Reused recedes. It brightens on hover so it is still
                            // inspectable — quiet, not disabled.
                            "border-dashed border-border bg-surface/40 opacity-55 hover:opacity-100 hover:border-ink/25"
                    }`}
                    style={isRebuilt && !selected ? { ["--tick-color" as string]: "var(--color-signal)" } : undefined}
                  >
                    <div className="relative aspect-video w-full overflow-hidden border-b border-border bg-canvas">
                      <MediaThumb asset={asset} token={token} className="h-full w-full" />
                      {/* Status sits over the media so the title below owns its
                          full row. Sharing one row made the label truncate to a
                          single letter on a three-column layout. */}
                      <span className="absolute right-1.5 top-1.5 bg-canvas/85 backdrop-blur-sm">
                        <StatusPill status={node.status} />
                      </span>
                      {/* §18.9 activity is only informative while work is in
                          flight. On a terminal node it just repeats the status
                          pill directly above it. */}
                      {node.current_activity && !TERMINAL_STATUSES.has(node.status) ? (
                        <div className="absolute inset-x-0 bottom-0 bg-canvas/80 px-2 py-1 font-mono text-[9px] uppercase tracking-wider text-active">
                          {node.current_activity}
                        </div>
                      ) : null}
                    </div>

                    <div className="flex flex-1 flex-col gap-1.5 p-3">
                      <p
                        className={`text-xs font-semibold leading-snug tracking-tight ${
                          isReused ? "text-muted" : "text-ink"
                        }`}
                      >
                        {node.label}
                      </p>
                      <p className="truncate font-mono text-[10px] text-faint" title={node.stable_key}>
                        {node.stable_key}
                      </p>
                      {fault || impact ? (
                        <div className="mt-auto flex flex-wrap gap-1 pt-1">
                          {fault ? <StatusPill status="TEST_FAULT" label="TEST FAULT" /> : null}
                          {impact ? <StatusPill status={impact} /> : null}
                        </div>
                      ) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}
