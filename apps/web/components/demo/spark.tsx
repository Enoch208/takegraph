/**
 * The two small charts from the reference: a filled area curve and a bar row.
 *
 * Both are plain inline SVG. A charting library for two sparklines would be a
 * large dependency for a shape that is eleven lines of path arithmetic, and it
 * would render into a canvas that assistive technology cannot read.
 *
 * Neither component invents a shape when the data is missing — an empty series
 * renders an empty state, because a decorative curve on a dashboard that claims
 * to be evidence is exactly the thing this product argues against.
 */

import type { Bucket } from "@/lib/build-metrics";

/**
 * Cumulative completion. Monotonic by construction, so the curve only ever rises
 * — that is a property of the data, not a smoothing choice.
 */
export function SparkArea({
  series,
  className = "",
  title,
}: {
  series: Bucket[];
  className?: string;
  title: string;
}) {
  if (series.length < 2) {
    return (
      <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
        Awaiting first completion
      </p>
    );
  }

  const width = 100;
  const height = 32;
  const peak = Math.max(...series.map((point) => point.value), 1);
  const step = width / (series.length - 1);
  const points = series.map((point, index) => {
    const x = index * step;
    const y = height - (point.value / peak) * height;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={className}
      role="img"
      aria-label={title}
    >
      <polygon
        points={`0,${height} ${points.join(" ")} ${width},${height}`}
        fill="currentColor"
        opacity="0.16"
      />
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/**
 * One bar per node, height = attempts. A bar above the baseline is a node that
 * failed and recovered, which is the single most important thing this product
 * has to show — so those bars are the ones that get the accent.
 */
export function SparkBars({
  bars,
  className = "",
}: {
  bars: { key: string; label: string; attempts: number; healed: boolean }[];
  className?: string;
}) {
  if (bars.length === 0) {
    return (
      <p className="font-mono text-[10px] uppercase tracking-wider text-faint">No attempts yet</p>
    );
  }
  const peak = Math.max(...bars.map((bar) => bar.attempts), 1);
  return (
    <div className={`flex items-end gap-[3px] ${className}`}>
      {bars.map((bar) => {
        const ratio = bar.attempts / peak;
        return (
          <span
            key={bar.key}
            title={`${bar.label}: ${bar.attempts} attempt${bar.attempts === 1 ? "" : "s"}`}
            className={`min-h-[2px] flex-1 rounded-[1px] ${
              bar.healed ? "bg-signal" : bar.attempts > 0 ? "bg-ink/25" : "bg-ink/8"
            }`}
            style={{ height: `${Math.max(ratio * 100, 6)}%` }}
          />
        );
      })}
    </div>
  );
}
