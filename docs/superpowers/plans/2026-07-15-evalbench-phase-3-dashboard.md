# EvalBench Phase 3 — Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the placeholder with a data-first, BullshitBench-inspired dashboard that discovers suites from `/suites`, filters `/results`, renders categorical metrics as ranked stacked bars, and renders every metric/derived estimate in a sortable CI-bearing matrix.

**Architecture:** One client page owns URL-independent filter/view state and fetches only the two specified API resources. Small presentational components receive typed props and never know suite-specific metric keys; the active suite metadata and response `stacked` map determine rendering. Recharts draws the horizontal bars; semantic HTML draws the matrix.

**Tech Stack:** Existing Next.js App Router, strict TypeScript, Tailwind CSS, Recharts, native controls; no component library beyond optional shadcn/ui primitives and no new backend dependency.

## Global Constraints

- Do not change backend aggregation, store, runner, MetricRecord, or Suite contracts unless a Phase 2 API response demonstrably violates `SPEC.md`; report rather than redesign.
- The page must render future suites solely from `/suites` metadata and dynamic metric/derived maps. No checks such as `if (suite === "structured")`.
- Every model row visibly shows row `n`. Every matrix cell visibly shows its own `n`, estimate, and 95% CI; `n=0` renders “— (n=0),” never a fabricated zero.
- Stacked segment order is Clear, Partial, Failed, Refused. Values below 8% have no inline label and remain available in the tooltip.
- Color never carries meaning alone: segment order, percentage text, labels/tooltips, and family dots remain present.
- Visual style: off-white background, muted green/amber/red/gray, generous row spacing, no gradients, no decorative shadows.
- Phase gate: live API data for structured renders in both views; all controls work; `npm run lint/build` and backend tests pass; `make api` and `make web` start before `/clear`.
- `[STRONGER MODEL REVIEW]` marks visual/design judgment. Route those steps to a stronger model or human visual reviewer.

---

## Locked frontend types and component props

```ts
// web/lib/api.ts
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

export type StackedBreakdown = { metric_key: string; n: number; segments: Segment[] };
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

export async function fetchSuites(signal?: AbortSignal): Promise<SuiteDescriptor[]>;
export async function fetchResults(query: ResultsQuery, signal?: AbortSignal): Promise<ResultsResponse>;
```

```ts
// component contracts
type ScopeBarProps = {
  activeDomain: Domain;
  onDomainChange: (domain: Domain) => void;
  rowCount: number;
  filtersOpen: boolean;
  onToggleFilters: () => void;
};

type FilterControlsProps = {
  excludeRefusals: boolean;
  onExcludeRefusalsChange: (value: boolean) => void;
  windowDays: WindowDays;
  onWindowDaysChange: (value: WindowDays) => void;
  families: string[];
  selectedFamilies: Set<string>;
  onToggleFamily: (family: string) => void;
};

type ModelBarChartProps = { rows: AggregatedModelRow[]; metricKey: string };
type LeaderboardProps = { rows: AggregatedModelRow[]; suite: SuiteDescriptor };
type LegendProps = { includeRefusals: boolean };
```

Family colors use a single exported deterministic map in `ModelBarChart.tsx` (OpenAI blue, Anthropic rust, Gemini violet, XAI charcoal, OpenRouter teal, Voyage indigo, Cohere magenta, fallback slate). Segment colors use CSS variables shared by chart, legend, and tooltip.

## Task 1: Typed API client and response guards

**Files:**

- Create: `web/lib/api.ts`
- Modify: `.env.example`
- Modify: `web/app/page.tsx`

**Responsibilities:** Centralize API URL/query construction, abortable fetches, status errors, and minimal runtime shape checking so UI components consume stable types.

**Interfaces:** Produces all locked frontend types/functions above plus:

```ts
export type Domain = "overall" | "software" | "finance" | "legal" | "medical" | "physics";
export type WindowDays = 7 | 30 | 90 | "all";
export type ResultsQuery = {
  suite: string;
  domain: Domain;
  windowDays: WindowDays;
  excludeRefusals: boolean;
  families: string[];
};
```

- [ ] **Step 1: Add the public API base setting**

Append `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` to `.env.example`. In code, remove one trailing slash and default to `http://localhost:8000`; never expose provider keys through `NEXT_PUBLIC_*`.

- [ ] **Step 2: Implement exact query serialization**

`fetchResults` appends `suite`, `domain`, `exclude_refusals`; appends `window_days` only when not `all`; and appends each selected family as a repeated `families` parameter. An empty family list means no family restriction. Use `{cache:"no-store", signal}`. On non-2xx, throw `ApiError(status, message)` using a safe response detail.

- [ ] **Step 3: Add minimal runtime guards**

Check suite responses are arrays with string names/key arrays/display arrays. Check result root/rows and, for each estimate encountered, that `n` is numeric and all estimate/CI fields are number or null. Throw `ApiError(500,"Malformed API response")` on mismatch. Do not add a schema library.

- [ ] **Step 4: Temporarily wire a typed smoke state**

Make `page.tsx` a client component, fetch suites on mount with an `AbortController`, and render loading, a concise error with Retry button, empty state, or the registered suite names. This is temporary but compiling code, not a placeholder comment.

- [ ] **Step 5: Verify**

Run `npm --prefix web run lint` and `npm --prefix web run build`. Expected: both pass under strict TypeScript. Start API/web and verify the suite name appears.

- [ ] **Step 6: Commit**

```bash
git add .env.example web/lib/api.ts web/app/page.tsx
git commit -m "feat: add typed dashboard API client"
```

## Task 2: Scope and filter controls

**Files:**

- Create: `web/components/ScopeBar.tsx`
- Create: `web/components/FilterControls.tsx`
- Create: `web/components/Legend.tsx`
- Modify: `web/app/page.tsx`

**Responsibilities:** Implement the fixed domain row, rate mode, time window, family chips, filter collapse, and semantic legend with accessible native controls.

- [ ] **Step 1: Implement `ScopeBar`**

Render the six labels exactly `Overall / Software / Finance / Legal / Medical / Physics` as a `role="tablist"`; buttons carry `role="tab"`, `aria-selected`, and a visible active underline. Render `rowCount.toLocaleString()` followed by `records`, then a `Show Filters`/`Hide Filters` button with `aria-expanded`. Keep it horizontally scrollable on narrow screens.

- [ ] **Step 2: Implement `FilterControls`**

Rate mode is a two-button radiogroup: `All attempts` maps to `excludeRefusals=false`; `Exclude refusals` maps to true. Time window is a labeled native `<select>` with `7d`, `30d`, `90d`, `all`. Family chips are toggle buttons with `aria-pressed`, colored dot plus family text. A selected family set equal to all available families is normalized by the page to `[]` in API queries, meaning unrestricted.

- [ ] **Step 3: Implement `Legend`**

Render fixed order and both swatch/text for Clear, Partial, Failed, and (when included) Refused. Use the same CSS variables as the chart. No metric-specific copy.

- [ ] **Step 4: Wire page state and two result fetches**

State defaults: first suite returned, `overall`, all attempts, `30`, filters open, no restricted families. Fetch an unrestricted-family response to discover available family names and row count; fetch a restricted response only when the selected set is a strict nonempty subset. Preserve the union of families seen for the active suite/domain/window so deselecting a family never removes its re-enable chip. Reset selected families when suite changes; keep domain/time/rate mode.

- [ ] **Step 5: Verify controls against live API**

Run lint/build. With API/web running, change every domain and time option, toggle refusals, collapse filters, and toggle a family off then on. In browser network inspection, verify query parameters match state and stale fetches are aborted.

- [ ] **Step 6: Commit**

```bash
git add web/components/ScopeBar.tsx web/components/FilterControls.tsx web/components/Legend.tsx web/app/page.tsx
git commit -m "feat: add dashboard scope and filters"
```

## Task 3: Ranked horizontal stacked bars

**Files:**

- Create: `web/components/ModelBarChart.tsx`
- Modify: `web/app/page.tsx`
- Modify: `web/app/globals.css`

**Responsibilities:** Render one dynamically selected categorical-like metric as accessible ranked stacked rows with always-visible sample size and safe thin-slice labels.

**Interfaces:** `ModelBarChart` consumes only `rows` and `metricKey`; it reads `row.stacked[metricKey]` and does not inspect suite names or other metric keys.

- [ ] **Step 1: Implement chart data transformation**

For each row with a breakdown, produce `{model, model_family, n, clear, partial, failed, refused}` from segment percentages. Sort by clear descending, partial descending, model ascending. Preserve full LiteLLM model strings; display may wrap but not truncate away the provider prefix without a title/tooltip.

- [ ] **Step 2: Implement Recharts layout**

Use `ResponsiveContainer`, vertical `BarChart` with numeric X domain `[0,100]`, categorical Y, `layout="vertical"`, and four stacked `Bar`s in fixed order. Allocate row height at least 52px and left margin enough for model + `(n=…)`; on compact screens allow chart horizontal overflow rather than illegible labels. No grid decoration beyond a faint 0/25/50/75/100 reference axis if useful.

- [ ] **Step 3: Implement labels and tooltip**

Each segment label renderer returns `null` under `8`; otherwise render a centered rounded integer percent with sufficient contrast. Tooltip lists all four labels, percentage to one decimal, and count; it also states row `n`. The Y-axis tick is a custom SVG group containing the family dot, model, and `(n=<row.n>)` so sample size is never tooltip-only.

- [ ] **Step 4: Wire dynamic primary metric**

On the page, choose the first key in active suite `display_metrics` order that exists in any returned row's `stacked`; because structured metadata puts `schema_valid` first, it is the Phase 3 primary bar. If none exists, omit the chart and explain that continuous metrics are in the matrix. Render title `<Suite label> reliability` and subtitle naming the display metric; do not hardcode structured.

- [ ] **Step 5: Apply restrained base styling** `[STRONGER MODEL REQUIRED: dashboard aesthetics]`

Use off-white page background near `#f7f5ef`, near-black text, 1px warm-gray separators, muted segment colors, 16–20px row gaps, and a centered max-width content area. Remove decorative shadows, gradients, glass effects, giant hero typography, and card grids. Verify the chart still communicates when viewed in grayscale: segment order, inline percentages, legend, and tooltip labels must remain.

- [ ] **Step 6: Verify chart edge cases**

Use browser devtools/local mocked response to inspect `n=2` vs large `n`, 100/0/0/0, 94/3/2/1, no rows, long model strings, only refusals, and mobile width. Confirm sub-8% labels are absent inline and present in tooltip, no SVG clipping, and percentages remain in segment order.

- [ ] **Step 7: Commit**

```bash
git add web/components/ModelBarChart.tsx web/app/page.tsx web/app/globals.css
git commit -m "feat: render ranked stacked model bars"
```

## Task 4: Sortable metric and derived leaderboard

**Files:**

- Create: `web/components/Leaderboard.tsx`
- Modify: `web/app/page.tsx`
- Modify: `web/app/globals.css`

**Responsibilities:** Render all active suite metrics plus every derived field, format estimates consistently, and sort null-safe according to metric direction.

**Interfaces:**

```ts
type SortKey = { kind: "metric" | "derived"; key: string };
function formatValue(value: number, format: DisplayMetric["format"] | "milliseconds"): string;
function estimateLabel(estimate: Estimate, format: ...): string;
```

- [ ] **Step 1: Build columns dynamically**

Metric columns follow `suite.display_metrics` order, then any declared `metric_keys` missing metadata in key order, then the union of `row.derived` keys in sorted order. Labels: metadata labels; `cost_adjusted_quality → Cost-adjusted quality`; `p95_latency_ms → p95 latency`. Formats: percent multiplies by 100 and appends `%`; currency uses USD with enough decimals to avoid displaying a nonzero value as `$0`; milliseconds uses `ms`; number uses at most three decimals.

- [ ] **Step 2: Render CI-bearing cells with sample size**

For a populated estimate, render the estimate on the first line, `95% CI <low>–<high>` on the second, and `n=<estimate.n>` on the third or same muted line. For `n=0`/null, render `—` and `n=0`; do not show `$0`, `0%`, or a fake interval. Row header repeats family dot, full model, and row `n`.

- [ ] **Step 3: Implement stable sorting**

Column header buttons expose `aria-sort`. First click uses `higher_is_better` to choose descending for favorable metrics and ascending for unfavorable metrics; derived cost-adjusted defaults descending and p95 latency ascending. Repeated click toggles. Null estimates always sort last; ties break by model string. Default sort is the first display metric in its favorable direction.

- [ ] **Step 4: Make the matrix usable without decoration**

Use a semantic `<table>`, sticky first column/header, horizontal overflow, tabular numbers, understated row rules, and a clear focus ring. Do not heat-map cells by red/green; the direction arrow/text and values carry meaning.

- [ ] **Step 5: Wire page and verify**

Render the leaderboard directly below the chart for every nonempty response. Test every column header, missing estimates, different `n` for `judge_variance`-shaped fixture, negative continuous CI, tiny currency, and mobile overflow. Run lint/build.

- [ ] **Step 6: Commit**

```bash
git add web/components/Leaderboard.tsx web/app/page.tsx web/app/globals.css
git commit -m "feat: add confidence interval leaderboard"
```

## Task 5: Final composition, states, and accessibility/visual review

**Files:**

- Modify: `web/app/layout.tsx`
- Modify: `web/app/page.tsx`
- Modify: `web/app/globals.css`
- Modify: `README.md`

**Responsibilities:** Finish page hierarchy, error/loading/empty behavior, responsive/a11y details, and document dashboard usage without adding features.

- [ ] **Step 1: Finalize layout and state transitions**

Page order is: compact product header; scope bar; suite title/subtitle; collapsible filters; legend; chart; leaderboard. On suite change, keep old data visually muted until new data arrives or show one stable skeleton area to avoid layout jump. Abort old fetches. Empty registered-suite state and empty filtered-results state have distinct concise copy. Error state names API connectivity and provides Retry; it never prints response bodies containing unknown content.

- [ ] **Step 2: Accessibility pass**

Ensure one `<h1>`, suite heading `<h2>`, visible keyboard focus, labels for select/radiogroup, status updates using `aria-live="polite"`, buttons rather than clickable divs, sufficient text/background contrast, and chart-equivalent information in the immediately following table. Respect `prefers-reduced-motion` and disable chart animation there.

- [ ] **Step 3: Visual review against §8** `[STRONGER MODEL REQUIRED: BullshitBench look and restraint]`

Review at 1440×900, 1024×768, and 390×844 with real structured response data. Check off-white canvas, restrained muted palette, generous row spacing, no gradients/decorative shadows, tabs/readout/filter hierarchy, readable model labels, sample size prominence, thin-slice behavior, and data-first density. Have a stronger model/human make only CSS/layout refinements; do not change API or add chart types.

- [ ] **Step 4: Document dashboard operation**

Add README instructions to start `make api` and `make web` in separate terminals, browser URL, the two views, filters, `n`/CI interpretation, and the dynamic-suite guarantee.

- [ ] **Step 5: Run the Phase 3 gate**

Run `uv run pytest backend/tests -q`, `npm --prefix web run lint`, `npm --prefix web run build`, and `git diff --check`. Expected: all pass.

Start `make api` and `make web` in separate terminals. Verify `/suites`, one `/results` query for each domain, all rate/window/family controls, stacked ranking, matrix sorting, visible row/cell `n`, and no bare estimate. Stop both servers.

- [ ] **Step 6: Commit and clear**

```bash
git add web/app README.md
git commit -m "feat: complete evalbench dashboard"
```

Record passing commands and commit hash. Do not touch latency suite in this session. Use `/clear` and open `docs/superpowers/plans/2026-07-15-evalbench-phase-4-latency-cost.md`.
