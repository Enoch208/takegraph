/**
 * Typed boundary for the control-plane API.
 *
 * PRD §23.2 forbids unbounded `any` and requires validation at external
 * boundaries. These shapes mirror the FastAPI response models; once the OpenAPI
 * generator is wired up they are replaced by generated types and this file keeps
 * only the fetch helpers.
 */

export type NodeDecision = {
  stable_key: string;
  label: string;
  decision: string;
  reason_code: string;
  reason: string;
  provider_calls: number;
};

export type DemoProof = {
  schema_version: string;
  /** Where the numbers came from. The UI must label the difference. */
  source: "TEMPLATE_PROJECTION" | "BUILD_EVENTS";
  /** True only when a real build produced and stored the referenced assets. */
  verified_build: boolean;
  template: string;
  total_nodes: number;
  reuse: number;
  rebuild: number;
  review: number;
  blocked: number;
  provider_calls: number;
  pricing_status: string;
  estimated_cost_usd: string | null;
  change_from: string;
  change_to: string;
  rebuild_nodes: NodeDecision[];
  plan_hash: string;
  graph_hash: string;
};

/** A failed fetch is surfaced, never smoothed into an empty result. */
export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

const API_BASE_URL = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

export async function fetchDemoProof(): Promise<Result<DemoProof>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/demo/proof`, {
      // The proof strip reflects seeded state, so a short revalidation window is
      // right. It must never be baked into the static build, or the page would
      // keep showing figures after the underlying build changed.
      next: { revalidate: 30 },
      headers: { accept: "application/json" },
    });

    if (!response.ok) {
      // PRD §0.1: never convert a failed dependency into a successful UI state.
      // The caller renders this string; it does not fall back to zeros.
      return {
        ok: false,
        error: `API returned ${response.status} ${response.statusText}`,
      };
    }

    return { ok: true, data: (await response.json()) as DemoProof };
  } catch (cause) {
    return {
      ok: false,
      error: cause instanceof Error ? cause.message : "Could not reach the control plane.",
    };
  }
}
