import Link from "next/link";

export default function SystemStatusPage() {
  return (
    <main id="main" className="min-h-svh bg-canvas px-6 py-16 text-ink">
      <div className="mx-auto max-w-xl border border-dashed border-border bg-surface p-8">
        <p className="font-mono text-[10px] uppercase tracking-wider text-faint">System</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Status</h1>
        <p className="mt-3 text-sm text-muted">
          Live readiness is reported by the control plane at{" "}
          <code className="font-mono text-ink">/health/ready</code>. This page does not invent
          green checks.
        </p>
        <div className="mt-6 flex gap-3">
          <Link href="/demo" className="text-xs uppercase tracking-wide text-signal">
            Open demo
          </Link>
          <Link href="/" className="text-xs uppercase tracking-wide text-muted">
            Landing
          </Link>
        </div>
      </div>
    </main>
  );
}
