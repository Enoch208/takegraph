/**
 * The reference's two filled accent cards — big figure, delta, sparkline.
 *
 * Its cards are solid purple against a light page. At our canvas brightness a
 * solid accent fill would dominate everything around it and would put white text
 * on a mid-tone, so the accent arrives as a wash and a border (`.panel-accent`)
 * and the figure keeps the page's text colour. Same emphasis, same role in the
 * hierarchy, without the contrast problem.
 *
 * The reference's delta chips are ±% against an unnamed period. This card takes
 * `caption` instead: a stated denominator beats a percentage nobody can source.
 */

import type { ReactNode } from "react";
import { Icon, type IconName } from "@/components/icon";

const ACCENT = {
  signal: "var(--color-signal)",
  verified: "var(--color-verified)",
  active: "var(--color-active)",
} as const;

const TEXT = {
  signal: "text-signal",
  verified: "text-verified",
  active: "text-active",
} as const;

export function FeatureCard({
  icon,
  label,
  value,
  caption,
  accent,
  children,
}: {
  icon: IconName;
  label: string;
  value: string;
  caption: string;
  accent: keyof typeof ACCENT;
  children?: ReactNode;
}) {
  return (
    <div
      className="panel panel-accent flex flex-col justify-between p-4"
      style={{ "--accent": ACCENT[accent] } as React.CSSProperties}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-xs text-muted">
            <Icon name={icon} className={`size-3.5 ${TEXT[accent]}`} />
            {label}
          </p>
          <p className="mt-3 font-display text-[26px] leading-none font-semibold tracking-tight tabular-nums">
            {value}
          </p>
        </div>
      </div>
      <div className={`mt-4 h-8 ${TEXT[accent]}`}>{children}</div>
      <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-faint">{caption}</p>
    </div>
  );
}
