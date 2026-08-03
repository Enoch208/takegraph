"use client";

import type { BuildNode } from "@/lib/api";
import { shortId } from "@/lib/api";
import { Icon } from "@/components/icon";
import { MediaThumb } from "@/components/demo/media-thumb";
import { StatusPill } from "@/components/demo/status-pill";

export function NodeDetail({
  node,
  token,
  onLiveRetake,
  liveBusy,
  liveError,
  isLiveBuild,
}: {
  node: BuildNode;
  token: string;
  onLiveRetake?: () => void;
  liveBusy?: boolean;
  liveError?: string | null;
  isLiveBuild?: boolean;
}) {
  const rejected = node.attempts.filter(
    (attempt) => attempt.status === "FAILED" || attempt.status === "TIMED_OUT",
  );
  const selected = node.selected_attempt;

  return (
    <aside className="flex w-full max-w-md shrink-0 flex-col border-l border-border bg-surface">
      <div className="border-b border-border px-4 py-4">
        <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Node detail</p>
        <h3 className="mt-1 text-lg font-semibold tracking-tight text-ink">{node.label}</h3>
        <p className="mt-1 font-mono text-[11px] text-muted">{node.stable_key}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <StatusPill status={node.status} />
          {isLiveBuild ? <StatusPill status="LIVE" label="LIVE" /> : null}
          {node.attempts.some((a) => a.is_injected_fault) ? (
            <StatusPill status="TEST_FAULT" label="TEST FAULT" />
          ) : null}
        </div>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4">
        {node.current_activity ? (
          <section>
            <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Activity</p>
            <p className="mt-1 text-sm text-active">{node.current_activity}</p>
          </section>
        ) : null}

        {node.reason || node.reason_code ? (
          <section className="border border-dashed border-border bg-elevated p-3">
            <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Reason</p>
            {node.reason_code ? (
              <p className="mt-1 font-mono text-[11px] text-signal">{node.reason_code}</p>
            ) : null}
            {node.reason ? <p className="mt-1 text-sm text-muted">{node.reason}</p> : null}
          </section>
        ) : null}

        <section>
          <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Fingerprint</p>
          <p className="mt-1 break-all font-mono text-[11px] text-muted">{node.fingerprint}</p>
        </section>

        {selected ? (
          <section>
            <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-faint">
              Selected attempt
            </p>
            <div className="border border-border bg-elevated p-3">
              <div className="flex flex-wrap gap-1.5">
                <StatusPill status={selected.status} />
                <StatusPill status={selected.mechanism} />
              </div>
              <p className="mt-2 font-mono text-[11px] text-muted">
                {selected.provider ?? "local"} · {selected.model ?? "—"}
              </p>
              {selected.assets[0] ? (
                <MediaThumb
                  asset={selected.assets[0]}
                  token={token}
                  className="mt-3 aspect-video w-full"
                />
              ) : null}
            </div>
          </section>
        ) : null}

        {rejected.length > 0 ? (
          <section>
            <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-faint">
              Rejected attempts
            </p>
            <div className="space-y-3">
              {rejected.map((attempt) => (
                <div key={attempt.id} className="border border-dashed border-danger/40 bg-elevated p-3">
                  <div className="flex flex-wrap gap-1.5">
                    <StatusPill status={attempt.status} />
                    {attempt.is_injected_fault ? (
                      <StatusPill status="TEST_FAULT" label="TEST FAULT" />
                    ) : null}
                    <StatusPill status={attempt.mechanism} />
                  </div>
                  <p className="mt-2 font-mono text-[11px] text-danger">
                    {attempt.error_code ?? attempt.error_class ?? "FAILED"}
                  </p>
                  {attempt.error_message ? (
                    <p className="mt-1 text-sm text-muted">{attempt.error_message}</p>
                  ) : null}
                  {attempt.assets.map((asset) => (
                    <div key={asset.id} className="mt-3">
                      <p className="mb-1 font-mono text-[10px] text-faint">
                        still playable · {shortId(asset.sha256)}
                      </p>
                      <MediaThumb asset={asset} token={token} className="aspect-video w-full" />
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </section>
        ) : null}

        {node.validations.length > 0 ? (
          <section>
            <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-faint">
              Validations
            </p>
            <ul className="space-y-2">
              {node.validations.map((validation) => (
                <li
                  key={validation.id}
                  className="flex items-center justify-between border border-border px-3 py-2"
                >
                  <span className="font-mono text-[11px] text-muted">{validation.gate_key}</span>
                  <StatusPill status={validation.status} />
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {node.stable_key === "video.clip.03" && onLiveRetake ? (
          <section className="border border-dashed border-signal/40 p-3">
            <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
              Guest live retake
            </p>
            <p className="mt-1 text-sm text-muted">
              Quota-limited LIVE generation off the critical path.
            </p>
            <button
              type="button"
              onClick={onLiveRetake}
              disabled={liveBusy}
              className="mt-3 inline-flex items-center gap-2 border border-signal bg-signal/10 px-3 py-2 text-xs font-medium uppercase tracking-wide text-signal transition-transform active:scale-95 disabled:opacity-50"
            >
              <Icon name="play" className="size-3.5" />
              {liveBusy ? "Requesting…" : "Request LIVE retake"}
            </button>
            {liveError ? <p className="mt-2 text-sm text-danger">{liveError}</p> : null}
          </section>
        ) : null}
      </div>
    </aside>
  );
}
