/**
 * The reference's "Key Metrics" card: a label, a proportional bar, and the two
 * numbers the bar sits between.
 *
 * Its bars are decorative gradients with no stated denominator. Here the bar is
 * `value / total` and both are printed, because a proportion a viewer cannot
 * check is a picture rather than a measurement.
 */

const TONE = {
  verified: "bg-verified",
  signal: "bg-signal",
  active: "bg-active",
  review: "bg-review",
  danger: "bg-danger",
  muted: "bg-muted",
} as const;

export type MetricTone = keyof typeof TONE;

export function MetricRow({
  label,
  value,
  total,
  tone = "muted",
}: {
  label: string;
  value: number;
  total: number;
  tone?: MetricTone;
}) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div className="grid grid-cols-[minmax(0,7rem)_1fr_auto] items-center gap-3">
      <span className="truncate text-xs text-muted">{label}</span>
      {/* The bar is presentation; the accessible value lives on the row. */}
      <span
        className="h-1.5 overflow-hidden rounded-full bg-ink/8"
        role="meter"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label={`${label}: ${value} of ${total}`}
      >
        <span
          className={`block h-full rounded-full ${TONE[tone]}`}
          style={{ inlineSize: `${pct}%` }}
        />
      </span>
      <span className="font-mono text-[11px] tabular-nums text-ink">
        {value}
        <span className="text-faint">/{total}</span>
      </span>
    </div>
  );
}
