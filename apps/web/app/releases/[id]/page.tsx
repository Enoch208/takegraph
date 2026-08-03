"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { Icon } from "@/components/icon";
import { IconRail } from "@/components/demo/icon-rail";
import { StatusPill } from "@/components/demo/status-pill";
import {
  createDemoSession,
  fetchDemoProject,
  fetchRelease,
  shortId,
  verifyRelease,
  type ReleaseDetail,
  type ReleaseVerification,
} from "@/lib/api";
import { readDemoSession, writeDemoSession } from "@/lib/demo-session";

/** A timestamp the API may not send must never crash the proof page.
 *  new Date(undefined).toISOString() throws RangeError, which took the whole
 *  release view down mid-verification. */
function iso(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "—" : d.toISOString();
}


export default function ReleaseProofPage() {
  const params = useParams<{ id: string }>();
  const releaseId = params.id;
  const [token, setToken] = useState<string | null>(null);
  const [release, setRelease] = useState<ReleaseDetail | null>(null);
  const [verification, setVerification] = useState<ReleaseVerification | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [verifying, startVerify] = useTransition();

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let session = readDemoSession();
      if (!session || session.expires_at * 1000 < Date.now() + 30_000) {
        const issued = await createDemoSession();
        if (!issued.ok) {
          if (!cancelled) setError(issued.error);
          return;
        }
        session = issued.data;
        writeDemoSession(session);
      }
      if (cancelled) return;
      setToken(session.access_token);
      const project = await fetchDemoProject(session.access_token);
      if (!project.ok) {
        if (!cancelled) setError(project.error);
        return;
      }
      const id = releaseId || project.data.release_id;
      const detail = await fetchRelease(id, session.access_token);
      if (!detail.ok) {
        if (!cancelled) setError(detail.error);
        return;
      }
      if (!cancelled) setRelease(detail.data);
    })();
    return () => {
      cancelled = true;
    };
  }, [releaseId]);

  const onVerify = () => {
    if (!token || !release) return;
    startVerify(async () => {
      const result = await verifyRelease(release.id, token);
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setVerification(result.data);
      setError(null);
    });
  };

  return (
    <div className="flex min-h-svh bg-canvas text-ink">
      <IconRail releaseHref={release ? `/releases/${release.id}` : undefined} />
      <main id="main" className="min-w-0 flex-1 overflow-y-auto px-6 py-8 md:px-10">
        <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
              Release proof
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight">
              {release ? release.version_label : "…"}
            </h1>
            {release ? (
              <div className="mt-3 flex flex-wrap gap-2">
                <StatusPill status={release.status} />
                {release.is_active ? (
                  <StatusPill status="PASSED" label="ACTIVE" />
                ) : (
                  <StatusPill status="REBUILD" label="SUPERSEDED" />
                )}
              </div>
            ) : null}
          </div>
          <div className="flex gap-2">
            <Link
              href="/demo"
              className="inline-flex items-center gap-2 border border-border px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted hover:text-ink"
            >
              <Icon name="demo" className="size-3.5" />
              Demo
            </Link>
            <button
              type="button"
              onClick={onVerify}
              disabled={!release || verifying}
              className="inline-flex items-center gap-2 border border-verified/50 bg-verified/10 px-3 py-2 text-xs font-medium uppercase tracking-wide text-verified transition-transform active:scale-95 disabled:opacity-40"
            >
              <Icon name="verified" className="size-3.5" />
              {verifying ? "Verifying…" : "Verify again"}
            </button>
          </div>
        </div>

        {error ? (
          <p className="mb-6 border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
            {error}
          </p>
        ) : null}

        {!release ? (
          <p className="font-mono text-xs uppercase tracking-wider text-muted">Loading release…</p>
        ) : (
          <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
            <section className="border border-dashed border-border bg-surface p-6">
              <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Assets</p>
              <ul className="mt-4 space-y-3">
                {release.assets.map((asset) => (
                  <li
                    key={asset.logical_path}
                    className="corner-ticks border border-border bg-elevated px-4 py-3"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-sm text-ink">{asset.logical_path}</p>
                      <p className="font-mono text-[10px] text-muted">{asset.role}</p>
                    </div>
                    <p className="mt-2 break-all font-mono text-[11px] text-verified">
                      sha256 {asset.sha256}
                    </p>
                    <p className="mt-1 font-mono text-[10px] text-faint">
                      {asset.size_bytes} bytes · {asset.mime_type}
                    </p>
                  </li>
                ))}
              </ul>
            </section>

            <section className="space-y-4">
              <div className="border border-border bg-surface p-5">
                <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
                  Provenance
                </p>
                <dl className="mt-3 space-y-3 text-sm">
                  <div>
                    <dt className="font-mono text-[10px] uppercase tracking-wider text-faint">
                      Build
                    </dt>
                    <dd className="font-mono text-muted">{shortId(release.build_id)}</dd>
                  </div>
                  <div>
                    <dt className="font-mono text-[10px] uppercase tracking-wider text-faint">
                      Revision
                    </dt>
                    <dd className="font-mono text-muted">
                      {shortId(release.project_revision_id)}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-mono text-[10px] uppercase tracking-wider text-faint">
                      Approved
                    </dt>
                    <dd className="text-muted">
                      {release.approved_at
                        ? `${iso(release.approved_at)} · ${
                            release.approved_by ? shortId(release.approved_by) : "—"
                          }`
                        : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-mono text-[10px] uppercase tracking-wider text-faint">
                      Published
                    </dt>
                    <dd className="text-muted">
                      {release.published_at
                        ? iso(release.published_at)
                        : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-mono text-[10px] uppercase tracking-wider text-faint">
                      Retention
                    </dt>
                    <dd className="font-mono text-review">
                      {release.retention_mode ?? "NOT_CONFIGURED"}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-mono text-[10px] uppercase tracking-wider text-faint">
                      Manifest
                    </dt>
                    <dd className="break-all font-mono text-[11px] text-muted">
                      {release.manifest_sha256 ?? "—"}
                    </dd>
                  </div>
                </dl>
              </div>

              {verification ? (
                <div className="border border-verified/40 bg-verified/10 p-5">
                  <p className="font-mono text-[10px] uppercase tracking-wider text-verified">
                    Verify again
                  </p>
                  <p className="mt-2 text-sm text-ink">
                    {verification.verified
                      ? `${verification.checked_assets} assets verified`
                      : "Verification failed"}
                  </p>
                  <p className="mt-2 font-mono text-[10px] text-muted">
                    {iso(verification.verified_at)}
                  </p>
                  <p className="mt-1 break-all font-mono text-[11px] text-muted">
                    {verification.manifest_sha256}
                  </p>
                  <p className="mt-2 font-mono text-[10px] text-review">
                    retention {verification.retention_mode}
                  </p>
                </div>
              ) : null}
            </section>
          </div>
        )}
      </main>
    </div>
  );
}
