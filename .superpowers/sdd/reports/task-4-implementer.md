# EvalBench Phase 3 Task 4 — Implementer Report

## Scope

Implemented Phase 3 Task 4 only: a suite-agnostic sortable leaderboard with confidence intervals and derived metrics. No backend, API client, aggregation, store, runner, or suite files were changed.

## Implementation

- Added `web/components/Leaderboard.tsx` with semantic table markup, dynamic metric/derived column construction, confidence-interval cell rendering, row and cell sample sizes, stable null-safe sorting, `aria-sort`, keyboard focus styling, model-family dots, and mobile horizontal overflow.
- Metric columns follow `display_metrics`, then declared `metric_keys` without display metadata. Derived columns are the sorted union of every row's `derived` keys.
- Added the required labels and formats for `cost_adjusted_quality` and `p95_latency_ms`; unknown derived keys use readable labels and numeric formatting.
- Added percent, currency, number, and millisecond formatting. Currency precision retains small non-zero values instead of presenting them as `$0`.
- Wired the leaderboard below the chart for every non-empty result response in `web/app/page.tsx`.
- Added restrained leaderboard styling to `web/app/globals.css`: sticky header/first column, tabular numbers, row rules, overflow, and visible focus rings without a heatmap.

## Manual fixture notes

These cases were reviewed against the Task 4 rendering and sorting rules and are suitable for a local mocked `/results` response or component fixture:

- **Dynamic columns:** a suite with display metrics `quality_score`, `schema_valid`, and a declared `judge_variance` metric missing display metadata renders those metric columns in that order; derived keys such as `cost_adjusted_quality` and `p95_latency_ms` follow in sorted key order.
- **Different sample sizes:** two populated estimates with different `n` values show each estimate's own `n` on the third line; the row header independently shows aggregate row `n`.
- **Empty estimate:** an estimate with `n=0` or `mean=null` renders `—` and its sample size, without a fabricated zero, interval, or currency value.
- **Negative continuous interval:** negative `ci_low`/`ci_high` values retain their signs and render in the same format as the mean.
- **Tiny currency:** a non-zero value below one cent retains sufficient decimal precision and does not display as `$0`.
- **Sorting:** the first display metric starts in its favorable direction; derived cost-adjusted quality starts descending, p95 latency starts ascending, missing estimates remain last in either direction, and equal estimates break by full model string.
- **Mobile width:** the table has a readable minimum width and uses horizontal scrolling rather than clipping model names or metric intervals.

## Checks

- `npm --prefix web run lint` — passed; npm emitted the environment's existing `http-proxy` configuration warning.
- `npm --prefix web run build` — passed; Next.js compiled, type-checked, and generated the static routes.
- `git diff --check` — passed.

## Commit

Feature commit: `feat: add confidence interval leaderboard`
