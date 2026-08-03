import { Icon } from "@/components/icon";

export type SseState = "connecting" | "live" | "reconnecting" | "idle" | "error";

export function SseStatus({ state, detail }: { state: SseState; detail?: string }) {
  const tone =
    state === "live"
      ? "text-verified"
      : state === "error"
        ? "text-danger"
        : state === "reconnecting" || state === "connecting"
          ? "text-active"
          : "text-muted";
  return (
    <div className={`flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider ${tone}`}>
      <span
        className={`size-1.5 rounded-full ${
          state === "live"
            ? "bg-verified animate-pulse"
            : state === "error"
              ? "bg-danger"
              : "bg-active"
        }`}
      />
      <Icon name="notification" className="size-3" />
      <span>{state}</span>
      {detail ? <span className="text-faint normal-case tracking-normal">{detail}</span> : null}
    </div>
  );
}
