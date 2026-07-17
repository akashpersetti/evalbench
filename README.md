# EvalBench

## Purpose

EvalBench is a provider-agnostic LLM evaluation platform. One shared harness runs pluggable benchmark suites across providers through LiteLLM, persists per-task metric records, and exposes results for future dashboard use.

## Current status

Three suites are registered and runnable: `structured`, `latency_cost`, and
`rag`. `structured` contains exactly 40 audited tasks: eight each in
`software`, `finance`, `legal`, `medical`, and `physics`. `latency_cost` and
`rag` have their own task sets under `backend/data/latency_cost/` and
`backend/data/rag/`, and declare their own `metric_keys` (see each suite
module under `backend/evalbench/suites/`).

### Dashboard

The web app is a working dashboard, not a placeholder. `/` renders a
leaderboard, bar chart, filter controls, and scope bar backed by the live
`GET /results` and `GET /suites` endpoints. `/run` lets an authenticated owner
trigger a suite run and poll its status, gated by magic-link email auth
(`POST /api/auth/request`, `GET /api/auth/verify`) against `POST
/runs/async`, `GET /runs/{run_id}`, and `GET /runs/{run_id}/status`.

A "Batch run" mode on the same page fans out one run per suite/domain pair
via `POST /runs/batch`: pick any subset of domains, check off one or more
suites, and give each checked suite its own comma-separated models list
(chat models for `structured`/`latency_cost`, `openai/text-embedding-3-small`
with a `::fixed_512`/`::recursive`/`::semantic` suffix for `rag`). The
endpoint validates every suite name before starting anything — one bad suite
name aborts the whole batch with no partial runs kicked off. The UI polls
every resulting run independently and lists each as `suite · domain —
status`.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.9 or newer
- npm (included with Node.js)

## Environment setup

Copy the example environment file to `.env` and add provider credentials as
needed:

```bash
cp .env.example .env
```

Keys are read from `.env`; never commit `.env` or database files. The supported
provider variables are `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, and `XAI_API_KEY`. The default judge is
`anthropic/claude-sonnet-4-5`. The default database URL is
`sqlite+aiosqlite:///./evalbench.db`, a local SQLite file accessed through
SQLAlchemy's async engine and session factory. Application startup creates the
schema and the API disposes the engine at shutdown. Both `.env` and
`evalbench.db` are ignored by Git.

## Commands

Install the project and development dependencies:

```bash
uv sync
```

Install the web dependencies:

```bash
npm --prefix web install
```

Run the backend tests:

```bash
uv run pytest backend/tests -q
```

The backend test suite uses fake or injected LiteLLM completion/embedding
callables and temporary SQLite databases. It makes no real LiteLLM or provider
calls, so running this command does not require credentials or incur model cost.

Lint and build the web placeholder:

```bash
npm --prefix web run lint
npm --prefix web run build
```

The Makefile exposes these targets:

- `make api` starts the FastAPI server on port 8000.
- `make web` starts the Next.js placeholder on port 3000.
- `make run-suite SUITE=<name> DOMAIN=<domain> MODELS="<model>[,<model>...]"`
  runs a registered suite and persists its records. `SUITE` and `DOMAIN` are
  required; `MODELS` defaults to `openai/gpt-4o` and accepts a comma-separated
  list.
- `make seed` runs `structured` for the `software` domain using `MODELS` from
  the environment or command line. If omitted, it uses the explicitly priced
  inexpensive default `openai/gpt-4o` (`$2.50` per million input tokens and
  `$10.00` per million output tokens, standard non-batch pricing).

`make seed` and `make run-suite` make real target and judge provider calls and
therefore require the relevant keys in `.env`; they can incur provider cost.
Tests use fakes and never invoke either API.

The Definition-of-Done structured run command is:

```bash
make run-suite SUITE=structured DOMAIN=software MODELS="openai/gpt-4o,anthropic/claude-sonnet-4-5"
```

To smoke the delivered services, use separate terminals. The API's
`/suites` response includes `structured` and its five metric keys; the web
response contains the `EvalBench` placeholder.

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

## Structured metrics and retry accounting

The structured suite emits exactly these five metric keys:

- `first_attempt_valid`: `1` when the initial response conforms to the schema,
  otherwise `0`.
- `schema_valid`: `1` when the response becomes valid, otherwise `0`.
- `retries_to_valid`: `0`, `1`, `2`, or `3` retry calls needed. “Up to 3×” is
  one initial call plus at most three retry calls. If all four attempts fail,
  this remains `3` and `schema_valid=0` identifies the failure.
- `retry_cost_usd`: the additional cost of retry completion calls only; it is
  `0` for a first-attempt success.
- `field_accuracy`: the fraction of expected fields with the correct type and
  value, using the shared judge only for declared free-text fields.

The runner's universal `latency_ms`, `prompt_tokens`, `completion_tokens`, and
`cost_usd` totals include every provider attempt, including retries.
`retry_cost_usd` is only the retry-only subset of that total cost.

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

The runner owns the universal execution fields. A suite populates only its
free-form `metrics` dictionary, whose keys must be the suite's declared
`metric_keys`.

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

`display_metrics` entries have exactly the keys `key`, `label`, `format`, and
`higher_is_better`. `format` must use the shared vocabulary:

- `percent` displays a proportion or percentage metric.
- `number` displays a numeric count, duration, or other non-currency value.
- `currency` displays a USD amount.

`detect_refusal` has a default heuristic and may be overridden. Suite methods
must return task data through these interfaces; they must not add database
columns or alter the core contracts.

## Dataset layout

Task data lives under `backend/data/<suite>/`. A suite's loader owns the file
format, but the structured suite uses one JSONL file per domain:

```text
backend/data/structured/
├── software.jsonl   # 8 tasks
├── finance.jsonl    # 8 tasks
├── legal.jsonl      # 8 tasks
├── medical.jsonl    # 8 tasks
└── physics.jsonl    # 8 tasks
```

Each structured line contains exactly the task `id`, `domain`, `prompt`,
`schema`, `expected`, `free_text_fields`, and `adversarial` fields. IDs are
`<domain>-NN`; `overall` loads all five domain files.

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

## Cloud deployment

`git push` to `main` triggers `.github/workflows/deploy.yml`, which builds the
Lambda package (`uv run python backend/deploy.py`, requires Docker) and runs
Terraform (`terraform/`) via a GitHub OIDC role — no long-lived AWS
credentials in CI. Terraform provisions the API and runner Lambdas, the
Terraform state and `evalbench.db` S3 buckets, SES for magic-link auth email,
and SSM parameters for provider keys, the judge model, and the admin token.

One-time bootstrap (state bucket, SES verification, SSM parameters, GitHub
secrets, first manual `terraform apply`, database migration to S3) is in
[docs/cloud-deploy.md](docs/cloud-deploy.md).

## Adding a suite

Adding a suite is an explicit three-file change:

1. Add its suite module under `backend/evalbench/suites/`.
2. Add one registration line/instance in `backend/evalbench/registry.py`.
3. Add its dataset directory under `backend/data/<suite>/`.

The runner, store schema, aggregation, and dashboard core should not need
suite-specific changes. A new suite must declare its metric keys and display
metadata, and all task executions still persist the exact `MetricRecord` shape
above.
