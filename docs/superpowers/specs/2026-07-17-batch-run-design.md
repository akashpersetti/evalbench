# Batch run — design

## Problem

Populating results across the full suite/domain/model matrix currently means
pasting shell loops (`make run-suite` invoked once per suite/domain/model
combination) and running them locally, one at a time. There's no way to kick
off the whole matrix from the web UI, and no way to watch progress across
many runs at once — only a single run's status is visible today.

## Goals

- One button in the web UI that fires the whole matrix as a set of async
  runs against the deployed cloud stack (runner Lambda / DynamoDB / S3),
  matching the existing `/runs/async` flow rather than running locally.
- Matrix is configurable from the UI: pick which suites to include, which
  domains to run them against, and (per suite) which models to use — not
  hardcoded to today's two loops.
- Live progress visible across all runs in the batch at once, console-style,
  built from existing status polling (no new log-streaming plumbing).

## Non-goals

- No raw Lambda log streaming into the browser.
- No server-side "batch" entity/table — the batch only exists as a list of
  `run_id`s the frontend holds in memory for the duration of the page visit.
  Refreshing the page loses the in-progress batch view (same as today: a
  single run's `run_id` is also just React state, not persisted client-side).
- No cross-suite model validation beyond what already exists (e.g. nothing
  stops a user from pointing a chat-model string at `rag` — same trust level
  as the existing single-run form).

## Design

### Backend: `POST /runs/batch`

Added to `backend/evalbench/api/app.py`, alongside the existing
`POST /runs/async`.

**Request:**

```json
{
  "domains": ["software", "finance", "legal", "medical", "physics"],
  "suites": [
    {"suite": "structured", "models": ["openai/gpt-4o", "anthropic/claude-sonnet-4-5"]},
    {"suite": "latency_cost", "models": ["openai/gpt-4o", "anthropic/claude-sonnet-4-5"]},
    {"suite": "rag", "models": ["openai/text-embedding-3-small::fixed_512", "openai/text-embedding-3-small::recursive", "openai/text-embedding-3-small::semantic"]}
  ],
  "judge_model": "anthropic/claude-sonnet-4-5"
}
```

`judge_model` is optional and, when present, applies to every run in the
batch (same field that already exists per-run today).

**Behavior:**

1. Resolve every `suite` name in the request via the existing
   `_resolve_suite` helper. If any is unknown, return `404` immediately
   (FastAPI's existing behavior for `_resolve_suite`) with no side effects —
   no `run_status` entries created, no Lambda invocations fired. Validation
   of the whole request happens before any run starts.
2. Same cloud-resource-configured checks `runs_async` already does
   (`dynamodb_run_status_table`, `runner_lambda_function` present) — `500` if
   either is missing, checked once for the whole batch before fan-out.
3. Compute the cross product of `suites × domains`: one run per
   `(suite, domain)` pair, each run bundling that suite's whole `models` list
   together — this is exactly how a single run already handles multiple
   models (`execute_run` loops over models internally within one run,
   tagging each metric record with its own model). A batch entry for `rag`
   with 3 chunk-strategy strings becomes *one* run per domain covering all 3
   strategies, not 3 separate runs — functionally identical result (one
   `metric_records` row per task/model regardless of how many runner Lambda
   invocations produced them), fewer Lambda invocations.
4. For each `(suite, domain)` pair, call a new shared helper:
   ```python
   def _start_run(suite: Suite, domain: str, models: list[str], judge_model: str | None) -> str:
       """Create the run_status entry and invoke the runner Lambda; return run_id."""
   ```
   Extracted from the body of `runs_async` (today: inline `uuid4()` +
   `suite.load_tasks` + `run_status.create_status` + `lambda_invoke.invoke_runner_async`
   at app.py:179–199) so both endpoints share one "start one run" code path.
   `runs_async` becomes a thin wrapper calling `_start_run` once;
   `/runs/batch` calls it once per `(suite, domain)` pair.
5. Return `{"runs": [{"run_id": "...", "suite": "...", "domain": "..."}, ...]}`
   — the suite/domain labels travel back so the frontend can label each row
   without an extra lookup.

No new DynamoDB table. Each run gets its own `run_status` item exactly as
`/runs/async` already produces — `/runs/batch` is purely a fan-out over the
existing per-run machinery.

### Frontend: batch mode on `/run`

Extends `web/app/run/page.tsx` rather than adding a new route, so the
existing magic-link auth flow (token in `localStorage`, verified via
`?magic=` query param) is reused as-is.

- A mode toggle above the form: **Single run** (today's form, unchanged) /
  **Batch run** (new).
- Batch form:
  - A shared domain checklist (the same 5 concrete domains as today's
    `DOMAINS` minus `"overall"`, since a run needs a concrete domain to load
    tasks — `"overall"` is a results-view-only aggregate, not a runnable
    domain).
  - One block per suite returned by `/suites`: a checkbox to include it in
    this batch, and a models text input (comma-separated, same parsing as
    the existing single-run `modelsInput`) that's only enabled when its
    suite checkbox is checked.
  - The existing judge-model field, applying to the whole batch.
- On submit: `POST /runs/batch` with the constructed body, receive
  `{runs: [{run_id, suite, domain}]}`, store as batch state
  (`BatchRun[] = {run_id, suite, domain, status: RunStatus | null}`).
- Polling: every 3s, fetch status for every `run_id` in the batch in
  parallel (`Promise.all` over `fetchRunStatus`), same 3s cadence as the
  existing single-run polling effect, stopping once every run reaches
  `done`/`error`.
- Rendering: a scrolling list, one row per `(suite, domain)`, console-style
  (monospace, dense), each row showing `pending` → `running X of Y` →
  `done`/`error` independently as its own poll result comes in — the same
  status vocabulary as the single-run view, just N rows instead of one.

### API client (`web/lib/api.ts`)

Add a `startBatch` function mirroring the existing `startRun`, posting to
`/runs/batch` and returning the parsed `{runs: [...]}` array, plus a
`BatchRun` type for the per-row shape.

## Error handling

- Invalid suite name anywhere in the request → `404` before any run starts
  (via `_resolve_suite`, same as today).
- Missing cloud config (no run-status table / runner function configured) →
  `500` before any run starts.
- A run that fails *after* being started (runner Lambda throws) already
  surfaces via `run_status.set_error` → the existing `status: "error"` state
  — the batch UI shows that row as errored; other rows keep polling
  independently. One run's failure doesn't block or cancel the rest of the
  batch (they were already fired as independent fire-and-forget Lambda
  invocations by the time any of them could fail).

## Testing

No existing precedent in this repo for FastAPI `TestClient`-based endpoint
tests — `backend/tests/test_cloud.py` tests the cloud helpers
(`run_status.create_status`, `lambda_invoke.invoke_runner_async`) directly
with monkeypatched boto3 clients, and that's the pattern this feature
follows: test `_start_run` and the batch validation/fan-out logic directly
(mocking `run_status`/`lambda_invoke`), not through HTTP. Covers:

- `_start_run` creates one `run_status` entry sized to `len(tasks) * len(models)`
  and invokes the runner Lambda once with the expected `RunConfig`.
- Batch fan-out invokes `_start_run` once per `(suite, domain)` pair —
  `len(suites) * len(domains)` total calls.
- An unknown suite name in the batch request raises before any
  `run_status.create_status` or `lambda_invoke.invoke_runner_async` call
  happens (asserting the mocks were never called).

Frontend: manual verification only (no existing frontend test suite in this
repo to extend).
