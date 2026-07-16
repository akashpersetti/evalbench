# EvalBench

## Purpose

EvalBench is a provider-agnostic LLM evaluation platform. One shared harness runs pluggable benchmark suites across providers through LiteLLM, persists per-task metric records, and exposes results for future dashboard use.

## Phase 1 status

Phase 1 delivers the dependency toolchain, core contracts, async store, runner,
judge, registry, and minimal API. It deliberately ships no registered benchmark
suite and no dashboard. Consequently, a fresh `GET /suites` returns `[]` and
there are no runnable suite names yet.

### Dashboard

The dashboard is Phase 3 work and is not delivered in Phase 1. The current web
application is only a runnable placeholder.

### `structured` suite

The `structured` suite and its dataset are Phase 2 work and are not delivered in
Phase 1.

### `latency_cost` suite

The `latency_cost` suite is Phase 4 work and is not delivered in Phase 1.

### `rag` suite

The `rag` suite is Phase 5 work and is not delivered in Phase 1.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

## Environment setup

Copy the example environment file and add provider credentials as needed:

```bash
cp .env.example .env
```

Keys are read from `.env`; do not commit `.env` or database files. The default
database URL is `sqlite+aiosqlite:///./evalbench.db`: one local SQLite file
accessed through SQLAlchemy's async engine and async session factory. Application
startup creates the schema; the API disposes the engine at shutdown. Both `.env`
and `evalbench.db` are ignored by Git.

## Commands

Install the project and development dependencies:

```bash
uv sync --dev
```

Run the backend tests:

```bash
uv run pytest backend/tests -q
```

The backend test suite uses fake or injected LiteLLM completion/embedding
callables and temporary SQLite databases. It makes no real LiteLLM or provider
calls, so running this command does not require credentials or incur model cost.

Install, lint, and build the Phase 1 web placeholder:

```bash
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run build
```

To smoke the delivered services, use separate terminals. The API's fresh
`/suites` response is `[]`; the web response contains `EvalBench`.

```bash
# terminal 1
make api

# terminal 2
curl -fsS http://127.0.0.1:8000/suites
```

Stop the API, then repeat for the web placeholder:

```bash
# terminal 1
make web

# terminal 2
curl -fsS http://127.0.0.1:3000
```

## MetricRecord contract

Every task execution produces exactly one `MetricRecord` with these fields:

```python
class MetricRecord(BaseModel):
    id: str
    run_id: str
    suite: str
    domain: str
    model: str
    provider: str
    model_family: str
    task_id: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error: str | None
    refused: bool
    metrics: dict[str, float]
    created_at: datetime
```

The runner owns universal execution fields. A suite populates only its free-form `metrics` dictionary.

## Async store

`metric_records` is the sole task-granularity table. It stores every
`MetricRecord` field as a portable SQLAlchemy type, with suite-specific values
only in the `metrics` JSON column. Its ordinary indexes are `run_id`, `suite`,
`domain`, `model_family`, and `created_at`; no suite-specific database columns
exist. Records are saved in explicit async transactions and reads are ordered by
`created_at`, `task_id`, and `model`.

## Suite contract

```python
class Suite(ABC):
    name: str
    metric_keys: list[str]
    display_metrics: list[dict]

    @abstractmethod
    def load_tasks(self, domain: str) -> list[Task]: ...

    @abstractmethod
    def build_prompt(self, task: Task) -> list[dict]: ...

    @abstractmethod
    def evaluate(self, task: Task, raw_output: str,
                 judge: Judge) -> dict[str, float]: ...

    def detect_refusal(self, raw_output: str) -> bool: ...
```

`display_metrics` entries have the keys `key`, `label`, `format`, and `higher_is_better`. `detect_refusal` has a default heuristic and may be overridden.

### Private execution context

`Task._execution_context` is a private, non-serialized runner implementation
detail. For one task/model execution, the runner attaches a generic context that
exposes metered completion/embedding operations plus immutable run, model, and
task identifiers; it clears the context in `finally`. This is the only private
assumption a future suite may use to request extra provider operations while the
runner remains responsible for timeouts, token/cost/latency accounting, error
records, and persistence. Suites must not persist, serialize, or branch core
logic on that context.

## Aggregate API contract

`GET /results` returns this exact top-level shape (once a future phase registers
a suite and records exist):

The request requires a `suite` query parameter. `domain` supports `overall`,
`software`, `finance`, `legal`, `medical`, and `physics` (default `overall`).
`window_days` may be omitted or set to `7`, `30`, or `90`.
`exclude_refusals` is a boolean (default `false`), and `families` is a repeated
query parameter for model-family filters. For example:

```text
GET /results?suite=structured&domain=software&window_days=30&exclude_refusals=true&families=OpenAI&families=Anthropic
```

```json
{
  "suite": "string",
  "domain": "string",
  "exclude_refusals": false,
  "rows": []
}
```

Each row has exactly `model`, `provider`, `model_family`, `n`, `metrics`,
`derived`, and `stacked`. Every entry in `metrics` and `derived` has this exact
estimate shape:

```json
{ "mean": 0.0, "n": 0, "ci_low": 0.0, "ci_high": 0.0 }
```

An unavailable estimate is `{ "mean": null, "n": 0, "ci_low": null,
"ci_high": null }`. `n` is metric-specific; the row-level `n` is the number of
selected raw records for that model/provider/family group. `derived` always
contains `p95_latency_ms`, and contains `cost_adjusted_quality` only when the
suite declares `quality_score`; its sample excludes zero-cost records.

`stacked` contains an entry only for a metric whose non-refused observed values
are all `0.0`, `0.5`, or `1.0`. Each entry has exactly this shape:

```json
{
  "metric_key": "string",
  "n": 0,
  "segments": [
    { "key": "clear", "label": "Clear", "count": 0, "percentage": 0.0 },
    { "key": "partial", "label": "Partial", "count": 0, "percentage": 0.0 },
    { "key": "failed", "label": "Failed", "count": 0, "percentage": 0.0 },
    { "key": "refused", "label": "Refused", "count": 0, "percentage": 0.0 }
  ]
}
```

The stacked `n` includes categorical observations and refusals; percentages are
`count / n * 100`. Passing `exclude_refusals=true` removes refusals before both
row and aggregate calculations.

### Confidence intervals

- Display metrics whose `format` is `percent` use a two-sided 95% Wilson
  interval with `z = 1.96`, bounded to `[0, 1]`.
- Other metric means, including `cost_adjusted_quality`, use the sample mean
  plus or minus `1.96 * sample_stdev / sqrt(n)`. With one observation, both
  interval endpoints equal the mean.
- `p95_latency_ms` uses the nearest-rank 95th percentile
  (`ceil(0.95 * n)`) and a 95% binomial order-statistic interval. With one
  observation, both endpoints equal the percentile.

Every aggregate therefore carries its own sample size and interval; consumers
must not render a bare mean.

## Adding a suite

1. Add the suite module under `backend/evalbench/suites/`.
2. Register one suite instance in the suite registry.
3. Add the suite dataset in its dataset directory.

The runner, store, and dashboard core should not need suite-specific changes.
