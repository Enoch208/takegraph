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
  source: "TEMPLATE_PROJECTION" | "BUILD_EVENTS";
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
  /** Short-lived signed URL for a poster frame from the verified build, or null
   *  when no build has produced one. Never a placeholder image. */
  poster_url: string | null;
};

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

export type DemoSession = {
  access_token: string;
  token_type: string;
  issued_at: number;
  expires_at: number;
  project_id: string;
};

export type DemoProject = {
  project_id: string;
  organization_id: string;
  slug: string;
  name: string;
  active_revision_id: string;
  legal_line: string;
  brief_text: string;
  build_id: string;
  build_status: string;
  release_id: string;
  release_version: string;
  release_status: string;
};

export type SelectedAsset = {
  id: string;
  role: string;
  ordinal: number;
  selected: boolean;
  sha256: string;
  size_bytes: number;
  mime_type: string;
  media_kind: string;
  derived_from_asset_id: string | null;
  verified_at: string | null;
  access_path: string;
};

export type AttemptEvent = {
  sequence: number;
  event_type: string;
  received_at: string;
  provider_timestamp: string | null;
};

export type Attempt = {
  id: string;
  attempt_no: number;
  parent_attempt_id: string | null;
  mechanism: string;
  provider: string | null;
  model: string | null;
  status: string;
  error_class: string | null;
  error_code: string | null;
  error_message: string | null;
  is_injected_fault: boolean;
  estimated_cost_usd: string | null;
  provider_reported_cost_usd: string | null;
  latency_ms: number | null;
  submitted_at: string | null;
  completed_at: string | null;
  created_at: string;
  events: AttemptEvent[];
  assets: SelectedAsset[];
};

export type Validation = {
  id: string;
  attempt_id: string | null;
  asset_id: string | null;
  policy_id: string | null;
  gate_key: string;
  gate_version: string;
  status: string;
  score: string | null;
  confidence: string | null;
  evidence: Record<string, unknown> | null;
  created_at: string;
};

export type BuildNode = {
  id: string;
  stable_key: string;
  label: string;
  node_type: string;
  status: string;
  current_activity: string | null;
  resolution: string | null;
  reason_code: string | null;
  reason: string | null;
  fingerprint: string;
  selected_asset_set_hash: string | null;
  source_build_node_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  selected_attempt: Attempt | null;
  attempts: Attempt[];
  selected_assets: SelectedAsset[];
  validations: Validation[];
};

export type BuildSummary = {
  id: string;
  project_id: string;
  project_revision_id: string;
  graph_revision_id: string;
  impact_plan_id: string | null;
  parent_build_id: string | null;
  status: string;
  total_nodes: number;
  reused_nodes: number;
  rebuilt_nodes: number;
  estimated_cost_usd: string | null;
  provider_reported_cost_usd: string | null;
  is_fixture: boolean;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  latest_event_sequence: number;
};

export type BuildGraph = {
  build: BuildSummary;
  latest_event_sequence: number;
  nodes: BuildNode[];
};

export type ChangeSet = {
  id: string;
  project_id: string;
  base_revision_id: string;
  patch: { parameters: { legal_line?: string; brief_text?: string } };
  status: string;
  expires_at: string;
  created_at: string;
};

export type ImpactNode = {
  stable_key: string;
  decision: string;
  reason_code: string;
  reason: string;
  old_fingerprint: string | null;
  new_fingerprint: string;
  provider_calls: number;
  estimated_cost_usd: string | null;
  requires_human_review: boolean;
};

export type ImpactPlan = {
  schema_version: string;
  plan_id: string;
  project_id: string;
  base_revision_id: string;
  proposed_revision_hash: string;
  graph_revision_id: string;
  nodes: ImpactNode[];
  summary: {
    reuse: number;
    rebuild: number;
    review: number;
    blocked: number;
    provider_calls: number;
    estimated_cost_usd: string | null;
    pricing_status: string;
  };
  plan_hash: string;
  expires_at: string;
};

export type ImpactCommit = {
  build_id: string;
  project_id: string;
  project_revision_id: string;
  graph_revision_id: string;
  status: string;
  total_nodes: number;
  reused_nodes: number;
  rebuilt_nodes: number;
};

export type LiveRetake = {
  build_id: string;
  build_node_id: string;
  stable_key: string;
  label: "LIVE";
  parent_build_id: string;
};

export type ReleaseAsset = {
  logical_path: string;
  role: string;
  sha256: string;
  size_bytes: number;
  mime_type: string;
  access_path: string;
};

export type ReleaseDetail = {
  id: string;
  project_id: string;
  build_id: string;
  version_label: string;
  status: string;
  manifest_asset_id: string | null;
  verification_asset_id: string | null;
  approved_by: string | null;
  approved_at: string | null;
  published_at: string | null;
  retention_mode: string | null;
  asset_count: number;
  is_active: boolean;
  project_revision_id: string;
  graph_revision_id: string;
  manifest_sha256: string | null;
  verification_sha256: string | null;
  assets: ReleaseAsset[];
};

export type ReleaseVerification = {
  release_id: string;
  verified: boolean;
  checked_assets: number;
  manifest_sha256: string;
  retention_mode: string;
  verified_at: string;
};

export type ApiErrorBody = {
  error?: {
    code?: string;
    message?: string;
    request_id?: string;
  };
};

const SERVER_API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

function apiRoot(): string {
  if (typeof window === "undefined") {
    return SERVER_API_BASE;
  }
  return "";
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ApiErrorBody;
    if (body.error?.message) {
      const code = body.error.code ? `${body.error.code}: ` : "";
      const rid = body.error.request_id ? ` (${body.error.request_id})` : "";
      return `${code}${body.error.message}${rid}`;
    }
  } catch {
    /* keep status text */
  }
  return `API returned ${response.status} ${response.statusText}`;
}

async function request<T>(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<Result<T>> {
  const { token, headers: initHeaders, ...rest } = init;
  const headers = new Headers(initHeaders);
  if (!headers.has("accept")) {
    headers.set("accept", "application/json");
  }
  if (token) {
    headers.set("authorization", `Bearer ${token}`);
  }
  try {
    const response = await fetch(`${apiRoot()}${path}`, { ...rest, headers });
    if (!response.ok) {
      return { ok: false, error: await parseError(response) };
    }
    if (response.status === 204) {
      return { ok: true, data: undefined as T };
    }
    return { ok: true, data: (await response.json()) as T };
  } catch (cause) {
    return {
      ok: false,
      error: cause instanceof Error ? cause.message : "Could not reach the control plane.",
    };
  }
}

export async function fetchDemoProof(): Promise<Result<DemoProof>> {
  return request<DemoProof>("/api/v1/demo/proof", {
    next: { revalidate: 30 },
  } as RequestInit);
}

export async function createDemoSession(): Promise<Result<DemoSession>> {
  return request<DemoSession>("/api/v1/demo/session", { method: "POST" });
}

export async function fetchDemoProject(token: string): Promise<Result<DemoProject>> {
  return request<DemoProject>("/api/v1/demo/project", { token });
}

export async function fetchBuildGraph(
  buildId: string,
  token: string,
): Promise<Result<BuildGraph>> {
  return request<BuildGraph>(`/api/v1/builds/${buildId}/graph`, { token, cache: "no-store" });
}

export async function fetchBuildSummary(
  buildId: string,
  token: string,
): Promise<Result<BuildSummary>> {
  return request<BuildSummary>(`/api/v1/builds/${buildId}`, { token, cache: "no-store" });
}

export function buildEventsUrl(buildId: string): string {
  return `${apiRoot()}/api/v1/builds/${buildId}/events`;
}

export async function createChangeSet(
  projectId: string,
  token: string,
  body: {
    base_revision_id: string;
    patch: { parameters: { legal_line: string } };
  },
): Promise<Result<ChangeSet>> {
  return request<ChangeSet>(`/api/v1/projects/${projectId}/change-sets`, {
    method: "POST",
    token,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function previewImpact(
  changeSetId: string,
  token: string,
): Promise<Result<ImpactPlan>> {
  return request<ImpactPlan>(`/api/v1/change-sets/${changeSetId}/impact`, {
    method: "POST",
    token,
  });
}

export async function commitImpactPlan(
  planId: string,
  token: string,
  planHash: string,
  idempotencyKey: string,
): Promise<Result<ImpactCommit>> {
  return request<ImpactCommit>(`/api/v1/impact-plans/${planId}/commit`, {
    method: "POST",
    token,
    headers: {
      "content-type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ plan_hash: planHash, start_build: true }),
  });
}

export async function requestLiveRetake(
  token: string,
  idempotencyKey: string,
): Promise<Result<LiveRetake>> {
  return request<LiveRetake>("/api/v1/demo/live-retake", {
    method: "POST",
    token,
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

export async function fetchRelease(
  releaseId: string,
  token: string,
): Promise<Result<ReleaseDetail>> {
  return request<ReleaseDetail>(`/api/v1/releases/${releaseId}`, { token, cache: "no-store" });
}

export async function verifyRelease(
  releaseId: string,
  token: string,
): Promise<Result<ReleaseVerification>> {
  return request<ReleaseVerification>(`/api/v1/releases/${releaseId}/verify`, {
    method: "POST",
    token,
  });
}

export type AssetAccess = {
  asset_id: string;
  access_url: string;
  expires_at: string;
};

export async function fetchAssetAccess(
  accessPath: string,
  token: string,
): Promise<Result<AssetAccess>> {
  return request<AssetAccess>(accessPath, { token, cache: "no-store" });
}

export function shortId(id: string): string {
  return id.replace(/-/g, "").slice(0, 8);
}
