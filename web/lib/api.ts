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
