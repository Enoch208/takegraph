import { Icon, type IconName } from "@/components/icon";
import type { DemoProof, Result } from "@/lib/api";

function MicroLabel({ children, tone = "muted" }: { children: string; tone?: "muted" | "signal" }) {
  return (
    <span
      className={`font-mono text-[10px] font-bold uppercase tracking-widest ${
        tone === "signal" ? "text-signal" : "text-faint"
      }`}
    >
      {children}
    </span>
  );
}

/**
 * System status. Every number here comes from GET /api/v1/demo/proof, which runs
 * the real impact engine — §4.4 forbids hard-coding seed metrics in React.
 *
 * When the API is unreachable this renders the failure. It does not fall back to
 * zeros or to a plausible-looking placeholder: §0.1 forbids converting a failed
 * dependency into a successful-looking UI state, and a demo that quietly shows
 * stale numbers is worse than one that admits it is disconnected.
 */
function SystemStatus({ proof }: { proof: Result<DemoProof> }) {
  return (
    <section className="corner-ticks relative flex flex-1 flex-col justify-between border border-dashed border-border bg-canvas p-8 transition-colors duration-500 hover:border-signal/40">
      <div className="space-y-7">
        <div className="flex items-center justify-between border-b border-dashed border-border pb-4">
          <MicroLabel tone="signal">[ System Status ]</MicroLabel>
          <div className="flex gap-1.5">
            <span className="size-1.5 bg-signal" />
            <span className="size-1.5 bg-border" />
            <span className="size-1.5 bg-border" />
          </div>
        </div>

        {!proof.ok ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-danger">
              <Icon name="review" className="size-4" />
              <span className="text-sm font-medium">Control plane unreachable</span>
            </div>
            <p className="font-mono text-xs leading-relaxed text-muted">{proof.error}</p>
            <p className="text-xs text-faint">
              These figures are read from the impact engine. Rather than show a placeholder, the
              page reports that it could not reach it.
            </p>
          </div>
        ) : (
          <>
            <div>
              <h3 className="mb-2 font-mono text-[10px] uppercase tracking-wider text-faint">
                Seed graph
              </h3>
              <div className="flex items-baseline gap-2">
                <p className="font-display text-5xl font-medium tracking-tighter">
                  {proof.data.total_nodes}
                </p>
                <span className="text-sm text-muted">nodes</span>
              </div>
              <p className="mt-2 text-xs text-muted">
                Changing “{proof.data.change_from}” to “{proof.data.change_to}”.
              </p>
            </div>

            <div className="rule-fade" />

            <dl className="grid grid-cols-2 gap-5">
              <div>
                <dt className="mb-1.5 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-faint">
                  <Icon name="reused" className="size-3 text-verified" />
                  Reused
                </dt>
                <dd className="font-display text-4xl font-medium tracking-tighter text-verified">
                  {proof.data.reuse}
                </dd>
              </div>
              <div>
                <dt className="mb-1.5 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-faint">
                  <Icon name="rebuild" className="size-3 text-signal" />
                  Rebuilt
                </dt>
                <dd className="font-display text-4xl font-medium tracking-tighter text-signal">
                  {proof.data.rebuild}
                </dd>
              </div>
            </dl>

            <div className="space-y-1.5 border-t border-dashed border-border pt-4 font-mono text-[11px]">
              <div className="flex justify-between">
                <span className="text-faint">provider calls</span>
                <span className="text-muted">{proof.data.provider_calls}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-faint">estimated cost</span>
                {/* §5.3 FR-IMPACT-003: unknown pricing stays unknown, never zero. */}
                <span className="text-review">{proof.data.estimated_cost_usd ?? "UNKNOWN"}</span>
              </div>
            </div>
          </>
        )}
      </div>

      {proof.ok && (
        <p className="mt-7 flex items-start gap-2 border border-review/25 bg-review/5 p-3 text-[11px] leading-relaxed text-review/90">
          <Icon name="review" className="mt-px size-3.5 shrink-0" />
          <span>
            {proof.data.verified_build
              ? "Derived from a real build's persisted events."
              : "Computed by the impact engine over the seed template. No build has run yet, so these are a projection rather than stored build evidence."}
          </span>
        </p>
      )}
    </section>
  );
}

/** Featured case study — the ORBIT production the demo actually operates on. */
function CaseStudy({ proof }: { proof: Result<DemoProof> }) {
  const poster = proof.ok ? proof.data.poster_url : null;
  const verified = proof.ok && proof.data.verified_build;

  return (
    <section className="group relative min-h-[480px] flex-1 overflow-hidden border border-border lg:flex-[1.6]">
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(120% 100% at 20% 0%, #16202b 0%, #0a0e13 45%, #050608 100%)",
        }}
      />
      {/* §18.5 wants a real ORBIT media preview. `poster_url` is a short-lived
          signed URL for the poster asset the verified build actually produced;
          when it is absent the card says so rather than dressing the space with
          stock imagery (§0.1). */}
      {poster && (
        <img
          src={poster}
          alt="Poster frame generated by the ORBIT baseline build"
          className="absolute inset-0 size-full object-cover object-[50%_38%] opacity-80 transition-opacity duration-700 group-hover:opacity-95"
        />
      )}
      {/* The generated poster carries its own burnt-in headline, so the lower
          third is taken to near-solid canvas — otherwise the card's title reads
          on top of the artwork's own type. */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "linear-gradient(to top, var(--color-canvas) 36%, color-mix(in oklab, var(--color-canvas) 70%, transparent) 58%, transparent 92%)",
        }}
      />

      <div className="absolute inset-0 flex flex-col justify-between p-8">
        <div className="flex items-start justify-between gap-3">
          <MicroLabel tone="signal">[ Featured Production ]</MicroLabel>
          {verified ? (
            <span className="flex items-center gap-1.5 border border-verified/40 bg-canvas/70 px-2 py-1 font-mono text-[10px] uppercase tracking-widest text-verified backdrop-blur-sm">
              <Icon name="verified" className="size-3" />
              Baseline built
            </span>
          ) : (
            <span className="border border-review/40 bg-canvas/70 px-2 py-1 font-mono text-[10px] uppercase tracking-widest text-review backdrop-blur-sm">
              Awaiting baseline build
            </span>
          )}
        </div>

        {!poster && (
          <div className="relative flex flex-1 items-center justify-center">
            <div className="relative flex size-40 items-center justify-center">
              <span className="absolute inset-0 rounded-full border border-active/25" />
              <span className="absolute inset-4 rounded-full border border-verified/20" />
              <span className="absolute size-2.5 rounded-full bg-active shadow-[0_0_20px_var(--color-active)]" />
            </div>
          </div>
        )}

        <div>
          <h3 className="mb-2 font-display text-2xl font-semibold tracking-tight">
            ORBIT Hydration
          </h3>
          <p className="max-w-md text-sm leading-relaxed text-muted">
            A four-shot cinematic launch package: 16:9 and 9:16 masters, narration, captions, end
            card and a poster — every asset traced to the source, prompt and model that produced it.
          </p>
          {proof.ok && (
            <p className="mt-3 font-mono text-[10px] uppercase tracking-widest text-faint">
              graph {proof.data.graph_hash.slice(0, 12)}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

const CAPABILITIES: { icon: IconName; title: string; body: string }[] = [
  { icon: "generate", title: "Generate", body: "Parallel provider runs with streamed events." },
  { icon: "validate", title: "Validate", body: "Technical, spec and identity gates per node." },
  { icon: "recover", title: "Recover", body: "Typed failures, bounded retries, real fallback." },
  { icon: "release", title: "Release", body: "Hash-verified manifests with retention read back." },
];

function CapabilityIndex() {
  return (
    <section className="flex flex-1 flex-col border border-border bg-canvas p-8">
      <div className="mb-6 flex items-center justify-between border-b border-dashed border-border pb-4">
        <MicroLabel tone="signal">[ Capabilities ]</MicroLabel>
        <MicroLabel>04</MicroLabel>
      </div>

      <ul className="flex flex-1 flex-col justify-between">
        {CAPABILITIES.map((capability, index) => (
          <li key={capability.title} className="group border-b border-border/60 py-4 last:border-0">
            <div className="flex items-start gap-3.5">
              <span className="mt-0.5 font-mono text-[10px] text-faint">
                {String(index + 1).padStart(2, "0")}
              </span>
              <Icon
                name={capability.icon}
                className="mt-0.5 size-4 shrink-0 text-muted transition-colors group-hover:text-signal"
              />
              <div>
                <h3 className="text-sm font-medium tracking-tight transition-colors group-hover:text-signal">
                  {capability.title}
                </h3>
                <p className="mt-1 text-xs leading-relaxed text-muted">{capability.body}</p>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ProofStrip({ proof }: { proof: Result<DemoProof> }) {
  return (
    <div
      id="proof"
      className="rise relative mx-auto flex w-full max-w-6xl scroll-mt-28 flex-col gap-6 px-6 pb-24 lg:flex-row"
      style={{ animationDelay: "0.3s" }}
    >
      <SystemStatus proof={proof} />
      <CaseStudy proof={proof} />
      <CapabilityIndex />
    </div>
  );
}
