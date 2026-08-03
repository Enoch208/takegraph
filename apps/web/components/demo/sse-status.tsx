import { Icon } from "@/components/icon";

export type SseState = "connecting" | "live" | "reconnecting" | "idle" | "error";

/**
 * The stream indicator.
 *
 * "Connecting" is only honest while a build is still producing events. On a
 * finished build the server has nothing left to send, so this sat on CONNECTING
 * indefinitely — which reads as a broken feature to anyone watching, and means
 * the opposite of what is true. When the build is terminal the label says so.
 *
 * State is carried by a word as well as a colour and a dot (§18.14).
 */
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);

export function SseStatus({
  state,
  buildStatus,
  detail,
}: {
  state: SseState;
  buildStatus?: string;
  detail?: string;
}) {
  const settled = buildStatus !== undefined && TERMINAL.has(buildStatus);
  const resolved: SseState = settled && state !== "error" ? "idle" : state;

  const label =
    resolved === "live"
      ? "Live"
      : resolved === "error"
        ? "Stream error"
        : resolved === "reconnecting"
          ? "Reconnecting"
          : resolved === "connecting"
            ? "Connecting"
            : settled
              ? "Stream closed"
              : "Idle";

  const tone =
    resolved === "live"
      ? "text-verified"
      : resolved === "error"
        ? "text-danger"
        : resolved === "idle"
          ? "text-muted"
          : "text-active";

  const dot =
    resolved === "live"
      ? "bg-verified animate-pulse"
      : resolved === "error"
        ? "bg-danger"
        : resolved === "idle"
          ? "bg-muted"
          : "bg-active animate-pulse";

  return (
    <div
      className={`flex items-center gap-2 rounded-full border border-hairline px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-wider ${tone}`}
      title={detail}
    >
      <span className={`size-1.5 rounded-full ${dot}`} />
      <Icon name={resolved === "error" ? "failed" : "notification"} className="size-3" />
      <span>{label}</span>
    </div>
  );
}
