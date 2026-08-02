/**
 * The hero backdrop: the ORBIT dependency graph itself, not decorative light.
 *
 * Deliberately SVG and CSS rather than WebGL. A canvas shader would need a
 * static fallback for machines without WebGL; this has nothing to fall back
 * from, ships no third-party runtime, adds no CDN request, and honours
 * `prefers-reduced-motion` through the same rule as everything else.
 *
 * The colours carry the product's meaning (§18.3): orange travels only the edges
 * a legal-copy change invalidates, everything else settles green as reused. Node
 * positions mirror the real §4.2 topology — sources left, keyframes and clips
 * through the middle, delivery at the right.
 */

type Node = { id: string; x: number; y: number; state: "reused" | "rebuild" };
type Edge = { from: string; to: string };

// Laid out by dependency depth, matching the compiled topological order.
const NODES: Node[] = [
  { id: "brief", x: 60, y: 190, state: "reused" },
  { id: "ref", x: 60, y: 300, state: "reused" },
  { id: "cutout", x: 190, y: 350, state: "reused" },
  { id: "plan", x: 210, y: 232, state: "reused" },
  { id: "kf1", x: 360, y: 90, state: "reused" },
  { id: "kf2", x: 360, y: 168, state: "reused" },
  { id: "kf3", x: 360, y: 246, state: "reused" },
  { id: "kf4", x: 360, y: 324, state: "reused" },
  { id: "clip1", x: 510, y: 90, state: "reused" },
  { id: "clip2", x: 510, y: 168, state: "reused" },
  { id: "clip3", x: 510, y: 246, state: "reused" },
  { id: "clip4", x: 510, y: 324, state: "reused" },
  { id: "music", x: 360, y: 402, state: "reused" },
  { id: "poster", x: 510, y: 22, state: "reused" },
  { id: "copy", x: 250, y: 470, state: "rebuild" },
  { id: "narration", x: 400, y: 470, state: "rebuild" },
  { id: "endcard", x: 400, y: 540, state: "rebuild" },
  { id: "delivery", x: 660, y: 400, state: "rebuild" },
];

const EDGES: Edge[] = [
  { from: "ref", to: "cutout" },
  { from: "brief", to: "plan" },
  { from: "ref", to: "plan" },
  ...(["kf1", "kf2", "kf3", "kf4"] as const).flatMap((kf) => [
    { from: "plan", to: kf },
    { from: "cutout", to: kf },
  ]),
  { from: "kf1", to: "clip1" },
  { from: "kf2", to: "clip2" },
  { from: "kf3", to: "clip3" },
  { from: "kf4", to: "clip4" },
  { from: "kf1", to: "poster" },
  { from: "brief", to: "music" },
  { from: "brief", to: "copy" },
  { from: "copy", to: "narration" },
  { from: "copy", to: "endcard" },
  ...(["clip1", "clip2", "clip3", "clip4", "music", "narration", "endcard"] as const).map((n) => ({
    from: n,
    to: "delivery",
  })),
];

const BY_ID = new Map(NODES.map((n) => [n.id, n]));

/** An edge is invalidated when it carries output from a node that will rebuild. */
function isInvalidated(edge: Edge): boolean {
  return BY_ID.get(edge.from)?.state === "rebuild";
}

function path(edge: Edge): string {
  const a = BY_ID.get(edge.from);
  const b = BY_ID.get(edge.to);
  if (!a || !b) return "";
  const mid = a.x + (b.x - a.x) * 0.5;
  return `M ${a.x} ${a.y} C ${mid} ${a.y}, ${mid} ${b.y}, ${b.x} ${b.y}`;
}

export function GraphBackdrop() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 overflow-hidden"
      // Fades the graph out at the edges so it never competes with the headline.
      style={{
        maskImage: "radial-gradient(120% 90% at 50% 42%, black 25%, transparent 78%)",
        WebkitMaskImage: "radial-gradient(120% 90% at 50% 42%, black 25%, transparent 78%)",
      }}
    >
      <svg
        viewBox="0 0 720 580"
        className="absolute left-1/2 top-1/2 h-[132%] w-[132%] -translate-x-1/2 -translate-y-1/2"
        fill="none"
      >
        {EDGES.map((edge, i) => {
          const invalid = isInvalidated(edge);
          return (
            <g key={`${edge.from}-${edge.to}`}>
              <path
                d={path(edge)}
                stroke={invalid ? "var(--color-signal)" : "var(--color-border)"}
                strokeWidth={invalid ? 1.1 : 0.8}
                opacity={invalid ? 0.5 : 0.55}
              />
              {invalid && (
                // The travelling dash is what makes causality visible. It runs
                // only on invalidated edges — never as a generic progress hint.
                <path
                  d={path(edge)}
                  stroke="var(--color-signal)"
                  strokeWidth={1.6}
                  strokeDasharray="4 130"
                  opacity={0.9}
                  style={{ animation: `flow ${5 + (i % 3)}s linear infinite` }}
                />
              )}
            </g>
          );
        })}

        {NODES.map((node, i) => {
          const rebuild = node.state === "rebuild";
          const color = rebuild ? "var(--color-signal)" : "var(--color-verified)";
          return (
            <g key={node.id}>
              {rebuild && (
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={9}
                  fill={color}
                  opacity={0.12}
                  style={{ animation: `breathe ${3 + (i % 3) * 0.4}s ease-in-out infinite` }}
                />
              )}
              <circle
                cx={node.x}
                cy={node.y}
                r={rebuild ? 3.4 : 2.6}
                fill={color}
                opacity={rebuild ? 0.95 : 0.55}
              />
            </g>
          );
        })}
      </svg>
    </div>
  );
}
