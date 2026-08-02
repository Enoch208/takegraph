import Image from "next/image";
import Link from "next/link";
import { Icon, type IconName } from "@/components/icon";
import type { DemoProof, Result } from "@/lib/api";

function SectionHeader({
  index,
  eyebrow,
  title,
  lit,
  body,
}: {
  index: string;
  eyebrow: string;
  title: string;
  lit: string;
  body: string;
}) {
  return (
    <div className="relative mb-14 flex w-full flex-col justify-between gap-8 md:flex-row md:items-end">
      <div className="flex max-w-3xl flex-col gap-6">
        <span className="text-sm font-medium uppercase tracking-widest text-signal">
          {index}. {eyebrow}
        </span>
        <h2 className="font-display text-4xl font-medium tracking-tighter sm:text-5xl md:text-6xl">
          {title} <span className="text-signal">{lit}</span>
        </h2>
        <p className="max-w-xl text-lg font-light leading-relaxed text-muted">{body}</p>
      </div>
      <div className="pointer-events-none absolute inset-x-0 -bottom-6 rule-fade" />
    </div>
  );
}

/** §18.5 act three: change → repair/recover → verified release. */
const ACTS: { icon: IconName; step: string; title: string; body: string; tone: string }[] = [
  {
    icon: "graph",
    step: "01",
    title: "Preview the blast radius",
    body: "Edit an approved line and TAKEGRAPH compiles the proposed revision, fingerprints every node, and shows exactly which outputs stop being valid — with a reason code per node, before a single provider call.",
    tone: "text-signal",
  },
  {
    icon: "recover",
    step: "02",
    title: "Repair and recover in bounds",
    body: "A timeout is classified, budgeted and routed to a fallback provider as a parent-linked child run. A failed identity check triggers a bounded retake. The rejected attempt stays inspectable — nothing is overwritten.",
    tone: "text-active",
  },
  {
    icon: "verified",
    step: "03",
    title: "Publish something checkable",
    body: "Release assets are hashed from stored bytes, not provider claims. The manifest, verification report and retention state are written to B2 and read back, so a third party can verify the release independently.",
    tone: "text-verified",
  },
];

export function HowItWorks({ proof }: { proof: Result<DemoProof> }) {
  return (
    <section
      id="how-it-works"
      className="rise relative mx-auto my-24 w-full max-w-7xl scroll-mt-28 px-6 pb-28"
    >
      <SectionHeader
        index="01"
        eyebrow="How it works"
        title="Causality, not"
        lit="guesswork."
        body="Every rebuild decision is explainable. The default is reuse; regeneration has to earn itself by proving that something a node depends on actually moved."
      />

      <div className="grid gap-6 md:grid-cols-3">
        {ACTS.map((act) => (
          <article
            key={act.step}
            className="corner-ticks group relative flex flex-col border border-border bg-surface/40 p-8 transition-colors duration-500 hover:border-white/20"
            style={{ ["--tick-color" as string]: "var(--color-border)" }}
          >
            <div className="mb-6 flex items-center justify-between">
              <Icon name={act.icon} className={`size-5 ${act.tone}`} />
              <span className="font-mono text-[10px] uppercase tracking-widest text-faint">
                {act.step}
              </span>
            </div>
            <h3 className="mb-3 font-display text-xl font-semibold tracking-tight">{act.title}</h3>
            <p className="text-sm leading-relaxed text-muted">{act.body}</p>
          </article>
        ))}
      </div>

      {proof.ok && (
        <div className="mt-8 border border-border bg-surface/30 p-8">
          <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-dashed border-border pb-4">
            <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-signal">
              [ Worked example · legal copy change ]
            </span>
            <span className="font-mono text-[10px] uppercase tracking-widest text-faint">
              plan {proof.data.plan_hash.slice(0, 12)}
            </span>
          </div>

          <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
            <div>
              <p className="mb-4 text-sm leading-relaxed text-muted">
                One approved phrase changes. The engine derives the affected set from the graph —
                it is not a hardcoded list.
              </p>
              <div className="space-y-2 font-mono text-xs">
                <div className="flex items-center gap-2">
                  <span className="text-faint">from</span>
                  <span className="text-muted line-through">{proof.data.change_from}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-faint">to</span>
                  <span className="text-signal">{proof.data.change_to}</span>
                </div>
              </div>
              <div className="mt-6 flex gap-6">
                <div>
                  <p className="font-display text-3xl font-medium tracking-tighter text-verified">
                    {proof.data.reuse}
                  </p>
                  <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
                    preserved
                  </p>
                </div>
                <div>
                  <p className="font-display text-3xl font-medium tracking-tighter text-signal">
                    {proof.data.rebuild}
                  </p>
                  <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
                    rebuilt
                  </p>
                </div>
              </div>
            </div>

            <ul className="space-y-px">
              {proof.data.rebuild_nodes.map((node) => (
                <li
                  key={node.stable_key}
                  className="flex flex-wrap items-center justify-between gap-2 border-l-2 border-signal bg-signal/[0.04] px-4 py-3"
                >
                  <div>
                    <p className="font-mono text-xs text-ink">{node.stable_key}</p>
                    <p className="mt-0.5 text-xs text-muted">{node.reason}</p>
                  </div>
                  <span className="font-mono text-[10px] uppercase tracking-wider text-signal/80">
                    {node.reason_code}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </section>
  );
}

const ARCHITECTURE: { icon: IconName; title: string; body: string }[] = [
  {
    icon: "lineage",
    title: "Genblaze orchestration",
    body: "Each generation node is its own named run, so lineage stays inspectable and cacheable. Retakes and cross-provider fallbacks are parent-linked child runs, not opaque retries.",
  },
  {
    icon: "storage",
    title: "Backblaze B2 as durable memory",
    body: "Content-addressed storage with read-after-write verification. A provider URL never satisfies a dependency — bytes are fetched, hashed and stored before a node can pass.",
  },
  {
    icon: "provider",
    title: "Provider portability",
    body: "Model choice comes from a versioned policy, so switching providers changes a policy hash and invalidates precisely the nodes that depended on it.",
  },
  {
    icon: "layers",
    title: "PostgreSQL is the truth",
    body: "Durable queue, leases and an append-only event log. Redis is an accelerator for live updates; losing it degrades latency, never correctness.",
  },
];

export function Architecture() {
  return (
    <section
      id="architecture"
      className="rise relative mx-auto my-24 w-full max-w-7xl scroll-mt-28 px-6 pb-28"
    >
      <SectionHeader
        index="02"
        eyebrow="Architecture"
        title="Storage is part of the build, not an"
        lit="export step."
        body="Recovery is a product feature. Timeouts, restarts, duplicate events and partial failures are expected states with defined behaviour — not incidents."
      />

      <div className="grid gap-6 sm:grid-cols-2">
        {ARCHITECTURE.map((item, index) => (
          <article
            key={item.title}
            className="group flex gap-5 border border-border bg-surface/30 p-8 transition-colors duration-500 hover:border-white/20"
          >
            <div className="flex size-10 shrink-0 items-center justify-center border border-border transition-colors group-hover:border-signal/40">
              <Icon name={item.icon} className="size-4 text-muted group-hover:text-signal" />
            </div>
            <div>
              <div className="mb-2 flex items-baseline gap-3">
                <h3 className="font-display text-lg font-semibold tracking-tight">{item.title}</h3>
                <span className="font-mono text-[10px] text-faint">
                  {String(index + 1).padStart(2, "0")}
                </span>
              </div>
              <p className="text-sm leading-relaxed text-muted">{item.body}</p>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

export function FinalCta() {
  return (
    <section id="product" className="rise relative mx-auto my-28 w-full max-w-5xl px-6 text-center">
      <h2 className="mx-auto mb-6 max-w-3xl font-display text-4xl font-medium leading-[1.1] tracking-tighter sm:text-5xl md:text-6xl">
        Generative tools make assets.
        <br />
        <span className="text-signal">TAKEGRAPH keeps a production alive.</span>
      </h2>
      <p className="mx-auto mb-10 max-w-xl text-lg font-light leading-relaxed text-muted">
        Open the seeded ORBIT build, change one approved line, and watch fourteen nodes stay exactly
        where they are.
      </p>
      <Link
        href="/demo"
        className="group relative inline-flex h-[54px] items-center justify-center gap-2 overflow-hidden bg-white/5 px-9 transition-transform active:scale-95"
      >
        <span
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              "radial-gradient(16% 50% at 50% 100%, var(--color-signal) 0%, transparent 100%)",
          }}
        />
        <span
          className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-700 group-hover:opacity-100"
          style={{
            background:
              "radial-gradient(60% 50% at 50% 100%, var(--color-signal) 0%, transparent 100%)",
            filter: "blur(16px)",
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
    </section>
  );
}

export function Footer() {
  return (
    <footer className="relative mt-20 overflow-hidden border-t border-border">
      <div className="mx-auto flex max-w-7xl flex-col gap-10 px-6 py-16 sm:flex-row sm:items-start sm:justify-between">
        <div className="max-w-sm">
          <Image
            src="/brand/lockup.png"
            alt="TAKEGRAPH — cause, remember, verify"
            width={1000}
            height={749}
            className="mb-5 h-24 w-auto opacity-90"
          />
          <p className="text-sm leading-relaxed text-muted">
            The self-healing build system for generative media.
          </p>
        </div>

        <div className="flex gap-14">
          <nav aria-label="Footer" className="flex flex-col gap-3">
            <span className="mb-1 font-mono text-[10px] uppercase tracking-widest text-faint">
              Product
            </span>
            {[
              { href: "#how-it-works", label: "How it works" },
              { href: "#architecture", label: "Architecture" },
              { href: "/demo", label: "Live build" },
            ].map((link) => (
              <a
                key={link.href}
                href={link.href}
                className="text-sm text-muted transition-colors hover:text-ink"
              >
                {link.label}
              </a>
            ))}
          </nav>
          <div className="flex flex-col gap-3">
            <span className="mb-1 font-mono text-[10px] uppercase tracking-widest text-faint">
              Built on
            </span>
            <span className="text-sm text-muted">Genblaze</span>
            <span className="text-sm text-muted">Backblaze B2</span>
          </div>
        </div>
      </div>

      {/* Oversized outlined wordmark — the template's closing device. */}
      <div className="pointer-events-none select-none px-6 pb-6">
        <p
          aria-hidden="true"
          className="font-display text-[15vw] font-bold leading-[0.78] tracking-tighter text-transparent"
          style={{ WebkitTextStroke: "1px color-mix(in oklab, var(--color-border) 90%, transparent)" }}
        >
          TAKEGRAPH
        </p>
      </div>
    </footer>
  );
}
