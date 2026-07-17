export type Domain =
  | "overall"
  | "software"
  | "finance"
  | "legal"
  | "medical"
  | "physics";

export type WindowDays = 7 | 30 | 90 | "all";

export type ResultsQuery = {
  suite: string;
  domain: Domain;
  windowDays: WindowDays;
  excludeRefusals: boolean;
  families: string[];
};

export type DisplayMetric = {
  key: string;
  label: string;
  format: "percent" | "number" | "currency";
  higher_is_better: boolean;
};

export type SuiteDescriptor = {
  name: string;
  metric_keys: string[];
  display_metrics: DisplayMetric[];
};

export type Estimate = {
  mean: number | null;
  n: number;
  ci_low: number | null;
  ci_high: number | null;
};

export type Segment = {
  key: "clear" | "partial" | "failed" | "refused";
  label: "Clear" | "Partial" | "Failed" | "Refused";
  count: number;
  percentage: number;
};

export type StackedBreakdown = {
  metric_key: string;
  n: number;
  segments: Segment[];
};

export type AggregatedModelRow = {
  model: string;
  provider: string;
  model_family: string;
  n: number;
  metrics: Record<string, Estimate>;
  derived: Record<string, Estimate>;
  stacked: Record<string, StackedBreakdown>;
};

export type ResultsResponse = {
  suite: string;
  domain: string;
  exclude_refusals: boolean;
  rows: AggregatedModelRow[];
};

const DEFAULT_API_BASE_URL = "http://localhost:8000";
export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || DEFAULT_API_BASE_URL
).replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isEstimateValue(value: unknown): value is Estimate {
  return (
    isRecord(value) &&
    (value.mean === null || isFiniteNumber(value.mean)) &&
    isFiniteNumber(value.n) &&
    (value.ci_low === null || isFiniteNumber(value.ci_low)) &&
    (value.ci_high === null || isFiniteNumber(value.ci_high))
  );
}

function isEstimateMap(value: unknown): value is Record<string, Estimate> {
  return isRecord(value) && Object.values(value).every(isEstimateValue);
}

function isDisplayMetric(value: unknown): value is DisplayMetric {
  return (
    isRecord(value) &&
    typeof value.key === "string" &&
    typeof value.label === "string" &&
    (value.format === "percent" ||
      value.format === "number" ||
      value.format === "currency") &&
    typeof value.higher_is_better === "boolean"
  );
}

function isSuiteDescriptor(value: unknown): value is SuiteDescriptor {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    Array.isArray(value.metric_keys) &&
    value.metric_keys.every((key) => typeof key === "string") &&
    Array.isArray(value.display_metrics) &&
    value.display_metrics.every(isDisplayMetric)
  );
}

function isSegment(value: unknown): value is Segment {
  return (
    isRecord(value) &&
    ((value.key === "clear" && value.label === "Clear") ||
      (value.key === "partial" && value.label === "Partial") ||
      (value.key === "failed" && value.label === "Failed") ||
      (value.key === "refused" && value.label === "Refused")) &&
    isFiniteNumber(value.count) &&
    isFiniteNumber(value.percentage)
  );
}

function isStackedBreakdown(value: unknown): value is StackedBreakdown {
  return (
    isRecord(value) &&
    typeof value.metric_key === "string" &&
    isFiniteNumber(value.n) &&
    Array.isArray(value.segments) &&
    value.segments.every(isSegment)
  );
}

function isAggregatedModelRow(value: unknown): value is AggregatedModelRow {
  return (
    isRecord(value) &&
    typeof value.model === "string" &&
    typeof value.provider === "string" &&
    typeof value.model_family === "string" &&
    isFiniteNumber(value.n) &&
    isEstimateMap(value.metrics) &&
    isEstimateMap(value.derived) &&
    isRecord(value.stacked) &&
    Object.values(value.stacked).every(isStackedBreakdown)
  );
}

function isResultsResponse(value: unknown): value is ResultsResponse {
  return (
    isRecord(value) &&
    typeof value.suite === "string" &&
    typeof value.domain === "string" &&
    typeof value.exclude_refusals === "boolean" &&
    Array.isArray(value.rows) &&
    value.rows.every(isAggregatedModelRow)
  );
}

async function responseError(response: Response): Promise<ApiError> {
  let message = `API request failed (${response.status})`;

  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && typeof payload.detail === "string") {
      const detail = payload.detail.trim();
      if (detail) {
        message = detail;
      }
    }
  } catch {
    // A non-JSON error response is represented by the safe status message.
  }

  return new ApiError(response.status, message);
}

async function fetchJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    cache: "no-store",
    signal,
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  try {
    return await response.json();
  } catch {
    throw new ApiError(500, "Malformed API response");
  }
}

export async function fetchSuites(
  signal?: AbortSignal,
): Promise<SuiteDescriptor[]> {
  const payload = await fetchJson("/suites", signal);

  if (!Array.isArray(payload) || !payload.every(isSuiteDescriptor)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload;
}

export async function fetchResults(
  query: ResultsQuery,
  signal?: AbortSignal,
): Promise<ResultsResponse> {
  const params = new URLSearchParams({
    suite: query.suite,
    domain: query.domain,
    exclude_refusals: String(query.excludeRefusals),
  });

  if (query.windowDays !== "all") {
    params.set("window_days", String(query.windowDays));
  }

  for (const family of query.families) {
    params.append("families", family);
  }

  const payload = await fetchJson(`/results?${params.toString()}`, signal);

  if (!isResultsResponse(payload)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload;
}

export type RunRequest = {
  suite: string;
  domain: Domain;
  models: string[];
  judgeModel?: string;
};

export type RunStatus = {
  run_id: string;
  status: "pending" | "running" | "done" | "error";
  completed: number;
  total: number;
  error?: string;
};

export type ConcreteDomain = Exclude<Domain, "overall">;

export type BatchSuiteInput = {
  suite: string;
  models: string[];
};

export type BatchRunRequest = {
  domains: ConcreteDomain[];
  suites: BatchSuiteInput[];
  judgeModel?: string;
};

export type BatchRunEntry = {
  run_id: string;
  suite: string;
  domain: string;
};

export type MetricRecord = {
  id: string;
  run_id: string;
  suite: string;
  domain: string;
  model: string;
  provider: string;
  model_family: string;
  task_id: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  error: string | null;
  refused: boolean;
  metrics: Record<string, number>;
  created_at: string;
};

function isRunStatus(value: unknown): value is RunStatus {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    (value.status === "pending" ||
      value.status === "running" ||
      value.status === "done" ||
      value.status === "error") &&
    isFiniteNumber(value.completed) &&
    isFiniteNumber(value.total)
  );
}

async function postJson(
  path: string,
  body: unknown,
  headers?: Record<string, string>,
): Promise<unknown> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  try {
    return await response.json();
  } catch {
    throw new ApiError(500, "Malformed API response");
  }
}

export async function requestMagicLink(email: string): Promise<void> {
  await postJson("/api/auth/request", { email });
}

export async function verifyMagicLink(token: string): Promise<string> {
  const payload = await fetchJson(
    `/api/auth/verify?token=${encodeURIComponent(token)}`,
  );

  if (
    !isRecord(payload) ||
    typeof payload.admin_token !== "string" ||
    !payload.admin_token
  ) {
    throw new ApiError(500, "Magic-link response did not include an admin token");
  }

  return payload.admin_token;
}

export async function startRun(
  config: RunRequest,
  adminToken: string,
): Promise<string> {
  const payload = await postJson(
    "/runs/async",
    {
      suite: config.suite,
      domain: config.domain,
      models: config.models,
      ...(config.judgeModel ? { judge_model: config.judgeModel } : {}),
    },
    { Authorization: `Bearer ${adminToken}` },
  );

  if (!isRecord(payload) || typeof payload.run_id !== "string") {
    throw new ApiError(500, "Malformed API response");
  }

  return payload.run_id;
}

function isBatchRunEntry(value: unknown): value is BatchRunEntry {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    typeof value.suite === "string" &&
    typeof value.domain === "string"
  );
}

function isBatchRunResponse(value: unknown): value is { runs: BatchRunEntry[] } {
  return (
    isRecord(value) &&
    Array.isArray(value.runs) &&
    value.runs.every(isBatchRunEntry)
  );
}

export async function startBatch(
  request: BatchRunRequest,
  adminToken: string,
): Promise<BatchRunEntry[]> {
  const payload = await postJson(
    "/runs/batch",
    {
      domains: request.domains,
      suites: request.suites,
      ...(request.judgeModel ? { judge_model: request.judgeModel } : {}),
    },
    { Authorization: `Bearer ${adminToken}` },
  );

  if (!isBatchRunResponse(payload)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload.runs;
}

export async function fetchRunStatus(runId: string): Promise<RunStatus> {
  const payload = await fetchJson(`/runs/${encodeURIComponent(runId)}/status`);

  if (!isRunStatus(payload)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload;
}

export async function fetchRun(runId: string): Promise<MetricRecord[]> {
  const payload = await fetchJson(`/runs/${encodeURIComponent(runId)}`);

  if (!Array.isArray(payload)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload as MetricRecord[];
}
