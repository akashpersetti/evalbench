# EvalBench Phase 3 Task 3 — Implementer Report

## Scope

Implemented Phase 3 Task 3 only: ranked horizontal stacked bars. No backend, aggregation, store, runner, or suite files were changed. The page now discovers the first display metric with a returned `stacked` breakdown and passes only the result rows plus metric key into `ModelBarChart`.

## Implementation

- Added `web/components/ModelBarChart.tsx` with a suite-agnostic Recharts vertical `BarChart`.
- Transforms `row.stacked[metricKey]` into Clear/Partial/Failed/Refused percentages and counts, then sorts by clear descending, partial descending, and model ascending.
- Uses a fixed 0–100 X axis, 64px-per-row chart sizing, responsive horizontal overflow, adaptive model-label space, family dots, visible `(n=...)`, full model titles, sub-8% label suppression, and a four-category tooltip with one-decimal percentages/counts.
- Moved segment colors to shared CSS variables in `web/app/globals.css` and centralized deterministic model-family colors in `ModelBarChart`; `FilterControls` now imports the same helper.
- Updated the page title/subtitle and chart wiring without suite-name conditionals.

## Manual fixture notes

These cases were reviewed against the chart transformation/rendering rules and are suitable for a local mocked `/results` response or devtools fixture:

- **`n=2`, `100/0/0/0`:** renders a full Clear bar, visible `100%`, and `(n=2)` on the model tick; the small sample is never hidden in a tooltip-only affordance.
- **`94/3/2/1`:** renders `94%` inline; the 3%, 2%, and 1% slices intentionally hide inline labels because each is below 8%, while the tooltip retains `Partial 3.0%`, `Failed 2.0%`, and `Refused 1.0%` with counts and row `n`.
- **No rows:** the page shows the existing no-results state; the chart component itself also has a no-breakdown empty state.
- **Long model strings:** the full LiteLLM string remains in the SVG tick text and SVG `<title>`, with adaptive left-axis space and a minimum chart width that permits horizontal scrolling rather than dropping the provider prefix.
- **Only refusals (`0/0/0/100`):** the fixed segment order remains Clear, Partial, Failed, Refused; only `100%` Refused is shown inline and all four categories remain in the tooltip.
- **Mobile width:** the chart keeps a readable minimum canvas width and exposes horizontal overflow, while the page and controls remain fluid.

## Checks

- `npm --prefix web run lint` — passed.
- `npm --prefix web run build` — passed.
- `git diff --check` — passed.

## Phase 3 Task 3 review follow-up

- Fixed Important I1 in `ModelBarChart`: `BarChart` now uses only an 8px left margin while `YAxis` retains the adaptive `axisWidth`, so model-label allocation is not counted twice and the stacked plot keeps its readable width.
- The scroll canvas now reserves `axisWidth + 520px` of plot/axis space plus the chart's left and right margins, preserving a 520px minimum plot area for both compact and long model labels.
- Reduced the category gap from 42% to 14% with a 64px row height, yielding approximately 18px between rows at the normal band size while retaining the existing single-row minimum height.

## Review follow-up checks

- `npm --prefix web run lint` — passed.
- `npm --prefix web run build` — passed.
- `git diff --check` — passed.
