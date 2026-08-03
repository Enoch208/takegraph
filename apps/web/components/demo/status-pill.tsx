import { Icon, type IconName } from "@/components/icon";

const STYLES: Record<string, { className: string; icon: IconName }> = {
  PASSED: { className: "border-verified/40 text-verified bg-verified/10", icon: "verified" },
  REUSED: { className: "border-verified/40 text-verified bg-verified/10", icon: "reused" },
  RUNNING: { className: "border-active/40 text-active bg-active/10", icon: "running" },
  QUEUED: { className: "border-active/40 text-active bg-active/10", icon: "running" },
  FALLBACK_PENDING: {
    className: "border-review/40 text-review bg-review/10",
    icon: "review",
  },
  RETAKE_PENDING: { className: "border-review/40 text-review bg-review/10", icon: "review" },
  FAILED: { className: "border-danger/40 text-danger bg-danger/10", icon: "failed" },
  BLOCKED: { className: "border-danger/40 text-danger bg-danger/10", icon: "failed" },
  REBUILD: { className: "border-signal/40 text-signal bg-signal/10", icon: "rebuild" },
  REUSE: { className: "border-verified/40 text-verified bg-verified/10", icon: "reused" },
  LIVE: { className: "border-signal/50 text-signal bg-signal/15", icon: "play" },
  TEST_FAULT: { className: "border-danger/50 text-danger bg-danger/15", icon: "failed" },
};

export function StatusPill({
  label,
  status,
}: {
  label?: string;
  status: string;
}) {
  const style = STYLES[status] ?? {
    className: "border-border text-muted bg-elevated",
    icon: "view" as IconName,
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${style.className}`}
    >
      <Icon name={style.icon} className="size-3" />
      {label ?? status.replaceAll("_", " ")}
    </span>
  );
}
