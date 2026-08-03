import { NextRequest } from "next/server";

/**
 * Explicit proxy for release verification.
 *
 * The catch-all rewrite in next.config cannot serve this route: verification
 * re-downloads every published asset from B2 and re-hashes it, which takes tens
 * of seconds, and the rewrite proxy hangs the socket up long before that. The
 * browser saw "Internal Server Error" while the API had in fact answered 200.
 *
 * A filesystem route wins over an `afterFiles` rewrite, so simply existing here
 * takes the path over, with a timeout matched to the work being done.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300;

const API_BASE = process.env.API_BASE_URL;
const VERIFY_TIMEOUT_MS = 240_000;

export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  if (!API_BASE) {
    return Response.json(
      { error: { code: "API_NOT_CONFIGURED", message: "This deployment has no API backend configured." } },
      { status: 503 },
    );
  }
  const { id } = await context.params;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), VERIFY_TIMEOUT_MS);
  try {
    const upstream = await fetch(`${API_BASE}/api/v1/releases/${id}/verify`, {
      method: "POST",
      headers: {
        // Forward only what the API needs; never the whole browser header set.
        authorization: request.headers.get("authorization") ?? "",
        "content-type": "application/json",
      },
      signal: controller.signal,
    });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (cause) {
    const timedOut = cause instanceof Error && cause.name === "AbortError";
    return Response.json(
      {
        error: {
          code: timedOut ? "VERIFICATION_TIMEOUT" : "UPSTREAM_UNAVAILABLE",
          message: timedOut
            ? "Verification did not finish within the allowed time."
            : "The verification service could not be reached.",
        },
      },
      { status: timedOut ? 504 : 502 },
    );
  } finally {
    clearTimeout(timer);
  }
}
