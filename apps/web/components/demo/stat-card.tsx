import { Icon, type IconName } from "@/components/icon";

/**
 * The KPI card from the reference layout: a tinted glyph chip, a large figure,
 * a quiet label.
 *
 * Two departures from the reference, both deliberate:
 *
 * - Its cards carry a "..." overflow menu. A control that opens nothing is worse
 *   than no control, so there isn't one.
 * - Its figures are decorative sizes. §18.14 requires status to survive without
 *   colour, so each card states its meaning in the label and the glyph, and the
 *   tint is the third signal rather than the only one.
 */

export type StatTone = "neutral" | "verified" | "signal" | "active" | "review" | "danger";

const TONE: Record<StatTone, { chip: string; figure: string }> = {
  neutral: { chip: "bg-elevated text-muted", figure: "text-ink" },
  verified: { chip: "bg-verified/12 text-verified", figure: "text-ink" },
  signal: { chip: "bg-signal/12 text-signal", figure: "text-ink" },
  active: { chip: "bg-active/12 text-active", figure: "text-ink" },
  review: { chip: "bg-review/12 text-review", figure: "text-ink" },
  danger: { chip: "bg-danger/12 text-danger", figure: "text-ink" },
};

export function StatCard({
  icon,
  label,
  value,
  detail,
  tone = "neutral",
}: {
  icon: IconName;
  label: string;
  value: number | string;
  detail?: string;
  tone?: StatTone;
}) {
  const styles = TONE[tone];
  return (
    <div className="panel p-4">
      <span
        className={`flex size-9 items-center justify-center rounded-[var(--radius-control)] ${styles.chip}`}
      >
        <Icon name={icon} className="size-[18px]" />
      </span>
      <p
        className={`mt-4 font-display text-[28px] leading-none font-semibold tracking-tight tabular-nums ${styles.figure}`}
      >
        {value}
      </p>
      <p className="mt-1.5 text-xs text-muted">{label}</p>
      {detail ? (
        <p className="mt-0.5 font-mono text-[10px] uppercase tracking-wider text-faint">{detail}</p>
      ) : null}
    </div>
  );
}
