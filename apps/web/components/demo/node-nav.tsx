import type { BuildNode, BuildSummary } from "@/lib/api";
import { shortId } from "@/lib/api";
import { StatusPill } from "@/components/demo/status-pill";

function groupKey(node: BuildNode): string {
  if (node.stable_key.startsWith("source.")) return "Sources";
  if (node.stable_key.startsWith("video.")) return "Video";
  if (node.stable_key.startsWith("image.") || node.stable_key.startsWith("graphic."))
    return "Image";
  if (node.stable_key.startsWith("audio.")) return "Audio";
  if (node.stable_key.startsWith("copy.") || node.stable_key.startsWith("plan.")) return "Text";
  return "Compose";
}

export function NodeNav({
  projectName,
  build,
  nodes,
  selectedKey,
  onSelect,
  counts,
}: {
  projectName: string;
  build: BuildSummary;
  nodes: BuildNode[];
  selectedKey: string | null;
  onSelect: (stableKey: string) => void;
  counts: { reused: number; rebuilt: number; running: number; failed: number };
}) {
  const groups = new Map<string, BuildNode[]>();
  for (const node of nodes) {
    const key = groupKey(node);
    const list = groups.get(key) ?? [];
    list.push(node);
    groups.set(key, list);
  }

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-surface">
      <div className="border-b border-border px-4 py-4">
        <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Project</p>
        <h2 className="mt-1 text-sm font-semibold tracking-tight text-ink">{projectName}</h2>
        <p className="mt-1 font-mono text-[10px] text-muted">build {shortId(build.id)}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <StatusPill status={build.status} />
          {build.is_fixture ? <StatusPill status="TEST_FAULT" label="REPLAY OF REAL RUN" /> : null}
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-2 font-mono text-[10px] uppercase tracking-wider text-muted">
          <div>
            <dt className="text-faint">Reuse</dt>
            <dd className="text-verified">{counts.reused}</dd>
          </div>
          <div>
            <dt className="text-faint">Rebuild</dt>
            <dd className="text-signal">{counts.rebuilt}</dd>
          </div>
          <div>
            <dt className="text-faint">Running</dt>
            <dd className="text-active">{counts.running}</dd>
          </div>
          <div>
            <dt className="text-faint">Failed</dt>
            <dd className="text-danger">{counts.failed}</dd>
          </div>
        </dl>
      </div>
      <div className="flex-1 overflow-y-auto px-2 py-3">
        {[...groups.entries()].map(([group, groupNodes]) => (
          <div key={group} className="mb-4">
            <p className="px-2 pb-1 font-mono text-[10px] uppercase tracking-wider text-faint">
              {group}
            </p>
            <ul className="space-y-0.5">
              {groupNodes.map((node) => {
                const fault = node.attempts.some((a) => a.is_injected_fault);
                const selected = node.stable_key === selectedKey;
                return (
                  <li key={node.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(node.stable_key)}
                      className={`flex w-full items-center justify-between gap-2 border px-2 py-2 text-left transition-colors ${
                        selected
                          ? "border-active/50 bg-active/10 text-ink"
                          : "border-transparent text-muted hover:border-border hover:bg-elevated hover:text-ink"
                      }`}
                    >
                      <span className="truncate text-xs">{node.stable_key}</span>
                      <span className="flex shrink-0 items-center gap-1">
                        {fault ? <StatusPill status="TEST_FAULT" label="FAULT" /> : null}
                        <span className="font-mono text-[9px] uppercase tracking-wider">
                          {node.status}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </aside>
  );
}
