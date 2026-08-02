import Link from "next/link";
import { GraphBackdrop } from "@/components/graph-backdrop";
import { Icon } from "@/components/icon";
import type { Result, DemoProof } from "@/lib/api";

/** The headline, with the words that carry the argument at full weight and the
 *  rest receded — the template's device, used here to say the actual thesis. */
const LINE_ONE = [
  { word: "Change", lit: false },
  { word: "one", lit: true },
  { word: "detail.", lit: true },
];
const LINE_TWO = [
  { word: "Rebuild", lit: false },
  { word: "only", lit: true },
  { word: "what", lit: true },
  { word: "changed.", lit: true },
];

function Words({ words, delay }: { words: typeof LINE_ONE; delay: string }) {
  return (
    <span className="rise flex flex-wrap justify-center gap-x-[0.26em]" style={{ animationDelay: delay }}>
      {words.map(({ word, lit }) => (
        <span key={word} className={lit ? "text-ink" : "text-ink/45"}>
          {word}
        </span>
      ))}
    </span>
  );
}

export function Hero({ proof }: { proof: Result<DemoProof> }) {
  return (
    <section className="relative flex min-h-[860px] w-full flex-col items-center overflow-hidden pt-36 md:pt-40">
      <GraphBackdrop />

      <div className="relative z-10 mx-auto mb-14 max-w-5xl px-6 text-center">
        <div
          className="rise mb-10 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] py-1.5 pl-3 pr-3.5 backdrop-blur-sm"
          style={{ animationDelay: "0.7s" }}
        >
          <span className="relative flex size-1.5">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-signal opacity-60" />
            <span className="relative inline-flex size-1.5 rounded-full bg-signal" />
          </span>
          <span className="text-[11px] font-medium tracking-wide text-ink/70">
            {proof.ok
              ? `${proof.data.template} · ${proof.data.total_nodes}-node seed graph`
              : "ORBIT Hydration seed graph"}
          </span>
        </div>

        <h1 className="mb-8 flex flex-col gap-y-1 font-display text-[2.75rem] font-medium leading-[1.08] tracking-tighter sm:text-6xl md:text-7xl lg:text-[5.2rem]">
          <Words words={LINE_ONE} delay="0.85s" />
          <Words words={LINE_TWO} delay="0.95s" />
        </h1>

        <p
          className="rise mx-auto mb-11 max-w-2xl text-lg font-light leading-relaxed text-muted md:text-xl"
          style={{ animationDelay: "1.1s" }}
        >
          TAKEGRAPH keeps multimodel media productions consistent, recoverable and verifiable. It
          knows what produced every output, preserves valid work, and rebuilds only what a change
          actually invalidated.
        </p>

        <div
          className="rise flex flex-col items-center justify-center gap-4 sm:flex-row"
          style={{ animationDelay: "1.25s" }}
        >
          <Link
            href="/demo"
            className="group relative inline-flex h-[52px] w-full min-w-[210px] items-center justify-center gap-2 overflow-hidden bg-white/5 px-7 transition-transform active:scale-95 sm:w-auto"
          >
            <span
              className="pointer-events-none absolute inset-0"
              style={{
                background:
                  "radial-gradient(14% 50% at 50% 100%, var(--color-signal) 0%, transparent 100%)",
              }}
            />
            <span
              className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-700 group-hover:opacity-100"
              style={{
                background:
                  "radial-gradient(58% 50% at 50% 100%, var(--color-signal) 0%, transparent 100%)",
                filter: "blur(14px)",
              }}
            />
            <span className="absolute inset-px bg-canvas" />
            <span className="relative z-10 flex items-center gap-2 text-xs font-medium uppercase tracking-wide">
              Open live build
              <Icon
                name="arrowRight"
                className="size-4 transition-transform duration-300 group-hover:translate-x-1"
              />
            </span>
          </Link>

          <a
            href="#how-it-works"
            className="group inline-flex h-[52px] w-full min-w-[210px] items-center justify-center gap-2 border border-border px-7 text-xs font-medium uppercase tracking-wide text-muted transition-colors hover:border-white/25 hover:text-ink sm:w-auto"
          >
            <Icon name="play" className="size-3.5" />
            See the change propagate
          </a>
        </div>
      </div>
    </section>
  );
}
