# EvalBench Phase 1 — Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the provider-agnostic backend core, async store, fixed contracts, minimal API, command-line workflow, and a placeholder web application, with all provider calls mocked in tests.

**Architecture:** `Suite` implementations provide task content, prompts, and metric evaluation; `runner.py` owns provider execution, universal measurements, error capture, and persistence. SQLAlchemy uses a single async engine/session factory, while blocking LiteLLM and synchronous suite evaluation run in worker threads under a bounded semaphore. A private, non-serialized task execution context is the one implementation assumption needed to reconcile the fixed synchronous `Suite.evaluate(...)` interface with later structured retries and RAG embedding calls; it is generic, never switches on suite names, and accounts for every call centrally.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pydantic-settings, LiteLLM, SQLAlchemy 2.0 async, aiosqlite, uv, pytest/pytest-asyncio; minimal Next.js App Router + TypeScript + Tailwind placeholder.

## Global Constraints

- `SPEC.md` is the only source of truth; do not add features, endpoints, persistence shapes, or suite-specific DB columns.
- Persist exactly one `MetricRecord` per `(run_id, task_id, model)` execution.
- Suites may populate only `metrics`; the runner owns `latency_ms`, token counts, `cost_usd`, `error`, and `refused`.
- SQLite URL is `sqlite+aiosqlite:///./evalbench.db`; use portable SQLAlchemy `String`, `Float`, `Integer`, `Boolean`, `DateTime(timezone=True)`, and `JSON` types only.
- All LiteLLM calls have a timeout, run behind a concurrency cap whose default is `4`, and turn exceptions into records rather than aborting a run.
- Keys come only from `.env`; no key value may be logged. Unknown model pricing returns `0.0` and logs one warning.
- Phase gate: backend tests pass, frontend builds, and both `make api` and `make web` start successfully before `/clear` and Phase 2.
- `[STRONGER MODEL REVIEW]` marks work involving design taste or unusually consequential judgment.

---

## Locked contracts for this and every later phase

Implement these names and types exactly. Later phase plans assume them and must not rename them.

```python
# backend/evalbench/models.py
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

class RunConfig(BaseModel):
    suite: str
    domain: str
    models: list[str]
    judge_model: str = "anthropic/claude-sonnet-4-5"

class SuiteResult(BaseModel):
    run_id: str
    records: list[MetricRecord]

class Estimate(BaseModel):
    mean: float | None
    n: int
    ci_low: float | None
    ci_high: float | None

class Segment(BaseModel):
    key: Literal["clear", "partial", "failed", "refused"]
    label: Literal["Clear", "Partial", "Failed", "Refused"]
    count: int
    percentage: float

class StackedBreakdown(BaseModel):
    metric_key: str
    n: int
    segments: list[Segment]

class AggregatedModelRow(BaseModel):
    model: str
    provider: str
    model_family: str
    n: int
    metrics: dict[str, Estimate]
    derived: dict[str, Estimate]
    stacked: dict[str, StackedBreakdown]

class ResultsResponse(BaseModel):
    suite: str
    domain: str
    exclude_refusals: bool
    rows: list[AggregatedModelRow]
```

```python
# backend/evalbench/suites/base.py
class Task(BaseModel):
    id: str
    domain: str
    prompt: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requires_generation: bool = True
    _execution_context: Any = PrivateAttr(default=None)

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

`requires_generation=False` is used only when a suite's unit under test is not a chat-completion model (RAG in Phase 5). It does not change the `Suite` contract. `_execution_context` is excluded from validation/serialization and exposes only generic provider-call methods and immutable run/model metadata.

```python
# backend/evalbench/runner.py
@dataclass(frozen=True)
class CallResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float

class ExecutionContext:
    run_id: str
    model: str
    task_id: str
    calls: list[CallResult]

    def complete(self, messages: list[dict]) -> CallResult: ...
    def embed(self, texts: list[str], *, embedder: str | None = None) -> list[list[float]]: ...

async def execute_run(
    config: RunConfig,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    completion_fn: Callable[..., Any] | None = None,
    embedding_fn: Callable[..., Any] | None = None,
) -> SuiteResult: ...
```

`ExecutionContext.complete()` and `.embed()` append usage/cost/latency to `calls`; suites never write universal record fields. The runner sums `calls` after evaluation. A structured retry therefore increases universal totals and also reports the retry-only subset in `metrics.retry_cost_usd`.

## Aggregation rules locked in Phase 1

For each model, filter records first by suite, domain (`overall` means no domain predicate), UTC cutoff, refusals, and family. Then:

- Row `n` is the number of filtered `MetricRecord`s for that model.
- Metric `n` is the number of rows containing that metric key; missing keys are omitted from that metric's sample. This is required for Phase 4's 20% `judge_variance` sample.
- A display metric whose `format == "percent"` uses the Wilson 95% interval, including fractional successes such as ties scored `0.5`:

```text
p = sum(x) / n
z = 1.96
denominator = 1 + z²/n
center = (p + z²/(2n)) / denominator
half = z * sqrt((p(1-p) + z²/(4n))/n) / denominator
ci = [max(0, center-half), min(1, center+half)]
```

- Every other metric uses the normal approximation around the sample mean. For `n > 1`, sample variance uses denominator `n-1`, `SE = sample_stdev / sqrt(n)`, and CI is `mean ± 1.96*SE`. For `n == 1`, return `[mean, mean]`. For no observations return `{mean:null,n:0,ci_low:null,ci_high:null}`.
- `derived.p95_latency_ms` uses nearest-rank p95 (`sorted_values[ceil(.95*n)-1]`). To avoid a bare estimate, its CI is the distribution-free order-statistic interval: lower/upper indices are the 2.5% and 97.5% binomial quantiles for `Binomial(n, .95)`, clamped to `[0,n-1]`; implement the binomial CDF directly with `math.comb`, with `[p95,p95]` for `n == 1`.
- When `quality_score` exists, `derived.cost_adjusted_quality` is the mean of per-record `quality_score / cost_usd` for records with `cost_usd > 0`, with the continuous normal CI above. Zero-cost rows are excluded from this derived metric, so its own `n` makes the omission visible.
- For each metric whose observed non-refusal values are all in `{0.0, 0.5, 1.0}`, return a stacked shape. `1.0 → clear`, `0.5 → partial`, `0.0 → failed`; refused rows always go to `refused` regardless of metric value. Segment order is always clear, partial, failed, refused. Percentages divide by stacked `n`, include zero-count segments, and total 100% apart from ordinary floating-point rounding.
- Matrix shape is `metrics` plus `derived`. No mean is ever returned without its own `n`, `ci_low`, and `ci_high`.

## Task 1: Project and dependency scaffolding

**Files:**

- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `README.md`
- Create: `backend/evalbench/__init__.py`
- Create: `backend/evalbench/api/__init__.py`
- Create: `backend/evalbench/suites/__init__.py`

**Responsibilities:** Configure Python 3.11+, the editable `backend/` package layout, runtime/test dependencies, pytest async mode, secret hygiene, and an initial README that states Phase 1 capabilities without claiming suites/dashboard are complete.

**Interfaces:** Produces an importable `evalbench` package and `uv run pytest backend/tests -q` command used by every later task.

- [ ] **Step 1: Create `pyproject.toml` with the fixed toolchain**

Use `[project] requires-python = ">=3.11"`; dependencies are `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `litellm`, `sqlalchemy>=2`, `aiosqlite`, and `python-dotenv`. Dev dependencies are `pytest`, `pytest-asyncio`, and `httpx`. Configure Hatchling to package `backend/evalbench`, and set pytest `asyncio_mode = "auto"`, `testpaths = ["backend/tests"]`, and `addopts = "--strict-markers"`. Do not add a second ORM, HTTP framework, or migration system.

- [ ] **Step 2: Create environment and ignore files**

`.env.example` must contain empty values for exactly `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, and `XAI_API_KEY`, plus `DATABASE_URL=sqlite+aiosqlite:///./evalbench.db`, `JUDGE_MODEL=anthropic/claude-sonnet-4-5`, `LITELLM_TIMEOUT_SECONDS=60`, and `MAX_CONCURRENCY=4`. `.gitignore` must cover `.env`, `evalbench.db`, Python caches/venvs, `.pytest_cache`, `web/node_modules`, and `web/.next`.

- [ ] **Step 3: Create package initializers and README skeleton**

Keep initializers side-effect-free. README sections: purpose, Phase 1 status, prerequisites, `.env` copy/setup, commands, exact `MetricRecord` fields, exact `Suite` signature, and “Adding a suite” steps limited to suite module + registry + dataset. Mark the dashboard and three suite sections as phase-delivery status statements, not implementation placeholders.

- [ ] **Step 4: Resolve and verify dependencies**

Run `uv sync --dev`. Expected: successful environment creation and lock resolution. Run `uv run python -c 'import fastapi, litellm, sqlalchemy; print("imports ok")'`. Expected: `imports ok`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .env.example .gitignore README.md backend/evalbench
git commit -m "chore: scaffold evalbench backend"
```

## Task 2: Configuration, provider metadata, and pricing

**Files:**

- Create: `backend/evalbench/config.py`
- Test: `backend/tests/test_runner.py`

**Responsibilities:** Load `.env`, expose one cached settings object, map model strings to allowed providers/families, hold explicit input/output price-per-million-token values, and compute cost without calling a network pricing service.

**Interfaces:**

- Produces `Settings`, `get_settings()`, `split_pipeline_model(model)`, `provider_for_model(model)`, `family_for_model(model)`, and `calculate_cost_usd(model, prompt_tokens, completion_tokens)`.
- `split_pipeline_model("openai/text-embedding-3-small::fixed_512")` returns `("openai/text-embedding-3-small", "fixed_512")`; non-pipeline models return `(model, None)`.

- [ ] **Step 1: Write failing configuration tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Test exact known mappings for OpenAI, Anthropic, Gemini, XAI, OpenRouter, and RAG aliases; composite-model splitting; positive known-model costs; and an unknown model returning `0.0` with a warning captured by `caplog`. Tests must pass explicit token counts and must not read real API keys.

- [ ] **Step 2: Run the focused test and confirm failure**

Run `uv run pytest backend/tests/test_runner.py -q`. Expected: collection/import failure because `config.py` does not exist.

- [ ] **Step 3: Implement configuration exactly**

`Settings(BaseSettings)` fields: the five optional secret keys, `database_url`, `judge_model`, `litellm_timeout_seconds: float = 60.0`, and `max_concurrency: int = Field(default=4, ge=1)`. Use `SettingsConfigDict(env_file=".env", extra="ignore")` and `@lru_cache` on `get_settings()`. Inside the cached loader, call `load_dotenv(override=False)` before constructing `Settings` so LiteLLM can read the same provider variables from `os.environ`; never copy or print their values.

Define explicit dictionaries for provider prefixes, family labels, model tiers, and pricing. Pricing entries are `(input_usd_per_million, output_usd_per_million)` and must include every model used in README examples, the default judge, and all Phase 5 embedders. Strip the `::chunk_strategy` suffix before lookup. `calculate_cost_usd` computes `(prompt_tokens*input_rate + completion_tokens*output_rate)/1_000_000`; unknown entries log the model name only and return `0.0`. `[STRONGER MODEL REVIEW: pricing is time-sensitive]` Have a stronger executor verify each literal rate against the provider's current official pricing page at implementation time and record the retrieval date in a nearby code comment; do not let LiteLLM fetch pricing dynamically.

Map direct Voyage/Cohere embedder aliases to `provider="openrouter"` to preserve the `MetricRecord.provider` vocabulary, while `model_family` remains `Voyage` or `Cohere` as required by Phase 5.

- [ ] **Step 4: Run focused tests**

Run `uv run pytest backend/tests/test_runner.py -q`. Expected: all configuration tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/config.py backend/tests/test_runner.py
git commit -m "feat: add provider and pricing configuration"
```

## Task 3: Pydantic contracts and stable suite base

**Files:**

- Create: `backend/evalbench/models.py`
- Create: `backend/evalbench/suites/base.py`
- Test: `backend/tests/test_suites.py`

**Responsibilities:** Implement the locked public record/API models and the fixed `Suite` ABC, plus the internal generic `Task` container and default refusal heuristic.

**Interfaces:** Produces every class shown in “Locked contracts.” `Suite.detect_refusal(raw_output)` performs a case-insensitive phrase check and has no provider dependency.

- [ ] **Step 1: Write failing contract tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Assert: a fully populated `MetricRecord` round-trips through `model_dump/model_validate`; missing any universal field fails validation; `RunConfig.models=[]` and unknown domains fail; default judge model is exact; `Task._execution_context` is absent from `model_dump`; a minimal fake `Suite` cannot omit abstract methods; refusal phrases (`"I can't assist"`, `"I cannot comply"`, `"as an AI"`) return true while ordinary answers return false.

Also create a generic registry-driven suite contract test that, for every registered suite, checks unique `metric_keys`, exact display dict keys `{key,label,format,higher_is_better}`, display keys are a subset of `metric_keys`, tasks have stable unique IDs, and `evaluate` returns only declared float-valued keys. It will collect zero suites in Phase 1 and automatically cover later registered suites without edits.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_suites.py -q`. Expected: imports fail for `models`/`base`.

- [ ] **Step 3: Implement models and validation**

Use UTC-aware datetimes. `RunConfig.domain` is a `Literal["overall","software","finance","legal","medical","physics"]`; `models` uses `Field(min_length=1)`. `MetricRecord.metrics` remains `dict[str,float]` without suite-specific validation. Implement the aggregate response types exactly as locked above.

- [ ] **Step 4: Implement `Task` and `Suite`**

Use the exact abstract signatures from §5. The default refusal heuristic normalizes whitespace/lowercase and checks a short constant tuple of refusal phrases. Do not parse metrics, call LiteLLM, or persist from `base.py`.

- [ ] **Step 5: Run focused tests**

Run `uv run pytest backend/tests/test_suites.py -q`. Expected: contract tests pass; the generic registered-suite parameter set is empty without error.

- [ ] **Step 6: Commit**

```bash
git add backend/evalbench/models.py backend/evalbench/suites/base.py backend/tests/test_suites.py
git commit -m "feat: define metric and suite contracts"
```

## Task 4: Async SQLAlchemy 2.0 store

**Files:**

- Create: `backend/evalbench/store.py`
- Test: `backend/tests/test_store.py`

**Responsibilities:** Own the async engine/session lifecycle, one portable table mirroring `MetricRecord`, atomic writes, and filtered raw-record queries. Aggregation stays out of this file.

**Interfaces:**

```python
class Base(DeclarativeBase): ...
class MetricRecordRow(Base):
    __tablename__ = "metric_records"

def create_engine(database_url: str | None = None) -> AsyncEngine: ...
def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]: ...
async def init_db(engine: AsyncEngine) -> None: ...
async def save_record(factory, record: MetricRecord) -> None: ...
async def save_records(factory, records: Sequence[MetricRecord]) -> None: ...
async def get_run_records(factory, run_id: str) -> list[MetricRecord]: ...
async def query_records(
    factory,
    *,
    suite: str,
    domain: str,
    window_days: int | None,
    exclude_refusals: bool,
    families: Sequence[str],
    now: datetime | None = None,
) -> list[MetricRecord]: ...
```

- [ ] **Step 1: Write failing async store tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Use `tmp_path` with `sqlite+aiosqlite:///<absolute-temp-path>` and a fresh engine per test. Cover schema creation, exact round-trip of JSON metrics/UTC timestamp, batch atomic insert, run isolation, suite/domain filtering, `overall` meaning no domain predicate, refusal/family filters, cutoff inclusion, and persistence after session closure. Dispose every engine in `finally`.

- [ ] **Step 2: Run and confirm failure**

Run `uv run pytest backend/tests/test_store.py -q`. Expected: import failure for `store.py`.

- [ ] **Step 3: Implement engine/session setup**

Call `sqlalchemy.ext.asyncio.create_async_engine(url, echo=False, pool_pre_ping=True)`. For in-memory SQLite tests, use `StaticPool`; do not use it for the file DB. Create sessions with `async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)`. The module may create lazy default `engine` and `SessionFactory`, but importing it must not create tables; FastAPI lifespan and CLI call `init_db`.

- [ ] **Step 4: Implement the portable row and conversion helpers**

Create one column for every `MetricRecord` field; `metrics` is SQLAlchemy `JSON`, timestamps are `DateTime(timezone=True)`, booleans are `Boolean`, and strings are `String`. Primary key is record `id`. Add ordinary indexes for `run_id`, `suite`, `domain`, `model_family`, and `created_at`. Implement private `_to_row(record)` and `_to_model(row)`; normalize SQLite-returned naive timestamps to UTC on read.

- [ ] **Step 5: Implement CRUD and filtering**

Use `async with factory() as session` and `async with session.begin()` for writes. Build `select(MetricRecordRow)` predicates without string SQL. Sort run records by `(created_at, task_id, model)` for deterministic output. `window_days=None` means all time; otherwise cutoff is `now - timedelta(days=window_days)` and uses `>=`.

- [ ] **Step 6: Run focused tests**

Run `uv run pytest backend/tests/test_store.py -q`. Expected: all store tests pass with no file created at repository root.

- [ ] **Step 7: Commit**

```bash
git add backend/evalbench/store.py backend/tests/test_store.py
git commit -m "feat: add async metric record store"
```

## Task 5: Generic judge and mockable provider-call layer

**Files:**

- Create: `backend/evalbench/judge.py`
- Create: `backend/evalbench/runner.py`
- Modify: `backend/tests/test_runner.py`

**Responsibilities:** Normalize LiteLLM responses, meter all target/embedding calls, provide generic JSON/text judge operations usable by all future suites, and expose no network activity during import or tests.

**Interfaces:**

```python
# judge.py
class Judge:
    def __init__(self, model: str, *, completion_fn=None,
                 timeout_seconds: float | None = None,
                 rng: random.Random | None = None): ...
    def complete_text(self, messages: list[dict]) -> str: ...
    def complete_json(self, messages: list[dict]) -> dict[str, Any]: ...
    def score_free_text(self, *, prompt: str, expected: str,
                        actual: str, rubric: str) -> float: ...

# runner.py, in addition to locked interfaces
def normalize_completion_response(response: Any, model: str, elapsed_ms: float) -> CallResult: ...
def normalize_embedding_response(response: Any) -> tuple[list[list[float]], int]: ...
```

- [ ] **Step 1: Write the no-network fake and failing tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Inside `test_runner.py`, define `FakeCompletion` with a `calls` list and `__call__(**kwargs)` returning `types.SimpleNamespace(choices=[...message.content...], usage=SimpleNamespace(prompt_tokens=..., completion_tokens=...))`. Define a matching fake embedding response with `.data[i].embedding` and `.usage.prompt_tokens`.

Every test must inject these fakes. Add one guard test that monkeypatches `litellm.completion` and `litellm.embedding` to functions raising `AssertionError("real LiteLLM call attempted")`, then verifies construction/import paths perform no calls. Test timeout is passed, response text/token normalization, config-table cost calculation, unknown usage defaults to zero, generic JSON judge parsing of fenced/unfenced JSON, and malformed judge JSON raises a named `JudgeResponseError`.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_runner.py -q`. Expected: missing `judge`/`runner` imports.

- [ ] **Step 3: Implement generic `Judge`**

Resolve the default callable inside `__init__`, not as a default argument, so monkeypatching works. Every call includes `model`, `messages`, and `timeout`. `complete_json` strips one optional markdown JSON fence and validates the decoded root is an object. `score_free_text` asks for only `{"score": <0..1>}`, clamps to `[0,1]`, and contains no provider/model-specific prompt text. Do not implement latency-suite pairwise policy here; Phase 4 owns rubric wording and A/B mapping through `complete_json`.

- [ ] **Step 4: Implement `CallResult` and `ExecutionContext`**

The context constructor receives injected sync callables, timeout, and a pricing function. `complete` measures `time.perf_counter`, calls `completion_fn(model=self.model, messages=messages, timeout=...)`, normalizes the response, and appends the result. `embed` strips any `::strategy` suffix or uses the explicit embedder, calls `embedding_fn(model=embedder, input=texts, timeout=...)`, records prompt tokens, zero completion tokens, calculated cost, and elapsed time, then returns vectors. Exceptions propagate to the runner after elapsed time is captured; never log request messages.

- [ ] **Step 5: Run focused tests**

Run `uv run pytest backend/tests/test_runner.py -q`. Expected: fake normalization/judge/context tests pass and no real provider function is called.

- [ ] **Step 6: Commit**

```bash
git add backend/evalbench/judge.py backend/evalbench/runner.py backend/tests/test_runner.py
git commit -m "feat: add mockable provider execution and judge"
```

## Task 6: Registry and suite-agnostic runner loop

**Files:**

- Create: `backend/evalbench/registry.py`
- Modify: `backend/evalbench/runner.py`
- Modify: `backend/tests/test_runner.py`
- Modify: `backend/tests/test_suites.py`

**Responsibilities:** Register suite instances explicitly, execute every task/model pair with bounded concurrency, persist even failures, and print non-secret progress.

**Interfaces:**

```python
# registry.py
SUITES: dict[str, Suite] = {}
def register_suite(suite: Suite) -> None: ...
def get_suite(name: str) -> Suite: ...
def list_suites() -> list[Suite]: ...

# runner.py
def _execute_one_sync(...) -> MetricRecord: ...
async def execute_run(...) -> SuiteResult: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

- [ ] **Step 1: Write failing runner-loop tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Create an in-test `FakeSuite` with two tasks, two metrics, and deterministic prompts/evaluation. Register it only inside a `monkeypatch.context()` or restore `SUITES` after each test. Cover the four records from two tasks × two models; UUID4-shaped IDs; common run ID; provider/family mapping; task domain preservation; metrics exactness; refusal detection; summed extra calls through `_execution_context`; `requires_generation=False`; timeout exception producing one record with `error="TimeoutError"`, zero tokens/cost, declared metrics set to `0.0`, and continuation to remaining work; database persistence matching returned records; and maximum observed concurrent fake calls never exceeding configured cap.

Do not use sleep to test concurrency. Use a thread-safe counter plus `threading.Event` barriers, and set the test cap to `2`.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_runner.py -q`. Expected: missing registry/loop behavior.

- [ ] **Step 3: Implement explicit registry**

`register_suite` rejects a duplicate name; `get_suite` raises `KeyError` with the unknown name and registered choices; `list_suites` sorts by suite name. Do not use filesystem scanning or import side effects. Keep the explicit import/registration block empty until Phase 2.

- [ ] **Step 4: Implement one execution**

Create an `ExecutionContext`, attach it to `task._execution_context`, call the first completion only when `task.requires_generation`, call `suite.evaluate`, then refusal detection, and finally construct the exact `MetricRecord`. Sum all context calls for universal totals. Use `datetime.now(timezone.utc)` and `str(uuid.uuid4())`.

On any target/evaluation/judge exception, set `error=type(exc).__name__`, preserve any already metered calls, set each declared metric absent so far to `0.0`, compute refusal from available text, and return the record. Clear `task._execution_context` in `finally` so task objects do not retain run state.

- [ ] **Step 5: Implement bounded async orchestration**

Create work items in task-major/model-minor deterministic order. Protect `await asyncio.to_thread(_execute_one_sync, ...)` with `asyncio.Semaphore(settings.max_concurrency)`, await all work, then sort back to original indices. Persist each record as it completes or one atomic ordered batch after `gather`; use the atomic batch for v1. Print `run_id`, task/model progress counts, and error class only—never prompts, outputs, headers, or keys.

- [ ] **Step 6: Implement CLI**

Use `argparse` flags `--suite`, `--domain`, `--models` (comma-separated), and `--judge-model`. Initialize DB, call `asyncio.run(execute_run(...))`, print the run ID, and return exit code `0` even when individual records contain errors; invalid CLI/config returns nonzero. The module guard calls `raise SystemExit(main())`.

- [ ] **Step 7: Run focused and full backend tests**

Run `uv run pytest backend/tests/test_runner.py backend/tests/test_suites.py -q`, then `uv run pytest backend/tests -q`. Expected: all pass; no outgoing API call and no root `evalbench.db` from tests.

- [ ] **Step 8: Commit**

```bash
git add backend/evalbench/registry.py backend/evalbench/runner.py backend/tests
git commit -m "feat: execute registered suites through shared runner"
```

## Task 7: Statistical aggregation for matrix and stacked-bar shapes

**Files:**

- Modify: `backend/evalbench/runner.py`
- Modify: `backend/tests/test_runner.py`

**Responsibilities:** Convert filtered raw records into the exact `ResultsResponse` rows consumed by the future dashboard, with correct per-cell sample sizes and confidence intervals.

**Interfaces:**

```python
def wilson_interval(values: Sequence[float]) -> Estimate: ...
def normal_mean_interval(values: Sequence[float]) -> Estimate: ...
def percentile_interval(values: Sequence[float], q: float) -> Estimate: ...
def aggregate_records(
    *, suite: Suite, records: Sequence[MetricRecord],
    domain: str, exclude_refusals: bool
) -> ResultsResponse: ...
```

- [ ] **Step 1: Write table-driven failing math tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Use hand-computed fixtures for all-success, all-failure, mixed binary, fractional ties, one continuous observation, multiple continuous observations, and empty input. Assert to `pytest.approx` against the formulas in this plan, not snapshots. Add aggregation fixtures with refusals, missing `judge_variance`, zero-cost quality rows, multiple families, and categorical values. Assert exact segment order/count/percentage, row `n`, metric-specific `n`, p95 `n`/CI, and cost-adjusted `n`.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_runner.py -k 'interval or aggregate' -q`. Expected: functions absent.

- [ ] **Step 3: Implement interval helpers exactly as locked above**

Use only the Python standard library `math` and `statistics`; do not add NumPy/SciPy. Clamp Wilson bounds to `[0,1]`. Do not clamp continuous bounds. Return the same `Estimate` shape even for empty inputs.

- [ ] **Step 4: Implement model grouping and matrix estimates**

Group by `(model, provider, model_family)`. Determine proportion metrics from the suite's `display_metrics` entry where `format == "percent"`; do not infer from values for CI selection. Aggregate only keys declared by `suite.metric_keys`, returning an empty estimate for a declared key with no observations. Always add p95 latency. Add cost-adjusted quality only when `quality_score` is declared.

- [ ] **Step 5: Implement stacked breakdowns**

For each declared metric independently, include a stacked result only if all present non-refusal values are in `{0.0,0.5,1.0}`. Add refusal rows to the gray segment. If refusals were excluded earlier, gray is present with count/percentage zero. Sort model rows by the first available stacked clear percentage descending, then model name; if no stacked metric exists, sort model name.

- [ ] **Step 6: Run focused and full tests**

Run `uv run pytest backend/tests/test_runner.py -q`, then `uv run pytest backend/tests -q`. Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/evalbench/runner.py backend/tests/test_runner.py
git commit -m "feat: aggregate metrics with confidence intervals"
```

## Task 8: Minimal FastAPI application

**Files:**

- Create: `backend/evalbench/api/app.py`
- Modify: `backend/tests/test_runner.py`

**Responsibilities:** Expose `/suites`, synchronous `/runs`, filtered aggregate `/results`, and raw `/runs/{run_id}` over the shared registry/runner/store.

**Interfaces:**

```python
GET /suites -> list[dict]
POST /runs (RunConfig) -> {"run_id": str}
GET /results?suite=&domain=&window_days=&exclude_refusals=&families= -> ResultsResponse
GET /runs/{run_id} -> list[MetricRecord]
```

- [ ] **Step 1: Write failing API tests** `[STRONGER MODEL REVIEW: Phase 1 test design]`

Use `httpx.AsyncClient` with `ASGITransport`, dependency overrides for a temp session factory and fake runner/provider. Cover empty `/suites`; suite metadata exact keys; unknown suite 404; POST validation; POST waiting until records persist; results filters including repeated `families` query parameters; legal window values `7,30,90` or omitted and rejection otherwise; raw run 404 when absent; and CORS allowing `http://localhost:3000`. Assert every returned estimate object has `mean,n,ci_low,ci_high` and every stacked row has `n`.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_runner.py -k api -q`. Expected: app import missing.

- [ ] **Step 3: Implement lifespan and dependencies**

Use FastAPI lifespan to call `init_db(default_engine)` and dispose it on shutdown. Expose `get_session_factory()` and `get_run_executor()` dependencies so tests never touch the real DB/network. Add CORS for only `http://localhost:3000`.

- [ ] **Step 4: Implement endpoints**

`/suites` returns sorted objects with exactly `name`, `metric_keys`, and `display_metrics`. `/runs` resolves the suite before execution and converts unknown suites to 404. `/results` resolves suite metadata, queries raw records with store filters, then calls `aggregate_records`. Parse `families` as repeated query values. `/runs/{run_id}` returns 404 when the list is empty.

- [ ] **Step 5: Run API and full tests**

Run `uv run pytest backend/tests -q`. Expected: all pass with fakes only.

- [ ] **Step 6: Commit**

```bash
git add backend/evalbench/api/app.py backend/tests/test_runner.py
git commit -m "feat: expose evalbench API"
```

## Task 9: Make targets and runnable placeholder web

**Files:**

- Create: `Makefile`
- Create: `web/package.json`
- Create: `web/tsconfig.json`
- Create: `web/next-env.d.ts`
- Create: `web/postcss.config.mjs`
- Create: `web/app/globals.css`
- Create: `web/app/layout.tsx`
- Create: `web/app/page.tsx`

**Responsibilities:** Make every required terminal command exist in Phase 1 and provide a deliberately minimal page that Phase 3 replaces.

**Interfaces:** `make api`, `make web`, `make seed`, and `make run-suite SUITE=... DOMAIN=... MODELS="..."`.

- [ ] **Step 1: Create `Makefile`**

Use `.PHONY`. `api` runs `uv run uvicorn evalbench.api.app:app --reload --port 8000`; `web` runs `npm --prefix web run dev`; `run-suite` validates nonempty `SUITE`, `DOMAIN`, and `MODELS` in shell and invokes `uv run python -m evalbench.runner --suite "$(SUITE)" --domain "$(DOMAIN)" --models "$(MODELS)"`; `seed` delegates to structured with a documented default demo model only after Phase 2, and in Phase 1 prints that structured is installed in Phase 2 and exits successfully. Do not put keys in commands.

- [ ] **Step 2: Create minimal Next/Tailwind package and configs**

Dependencies: Next App Router, React, React DOM, Recharts; dev dependencies TypeScript, Tailwind, PostCSS, ESLint, and the matching Next ESLint config. Scripts: `dev`, `build`, `start`, and `lint`. Use strict TypeScript, `@/*` path mapping, and Tailwind directives/import in `globals.css` according to the resolved Tailwind major version.

- [ ] **Step 3: Create accessible placeholder app**

`layout.tsx` exports metadata title `EvalBench` and wraps children in `<html lang="en">`. `page.tsx` renders only the product title and “Core API ready; structured dashboard arrives in Phase 3.” Do not start dashboard styling here.

- [ ] **Step 4: Install and build**

Run `npm --prefix web install`, then `npm --prefix web run build` and `npm --prefix web run lint`. Expected: both exit 0.

- [ ] **Step 5: Smoke test both long-running targets separately**

Terminal A: run `make api`, wait for startup, then in Terminal B run `curl -fsS http://127.0.0.1:8000/suites`; expected `[]`. Stop the API. Run `make web`, wait for startup, then `curl -fsS http://127.0.0.1:3000`; expected HTML containing `EvalBench`. Stop the web server. The phrase `make api && make web` in the spec means both targets must start; do not literally wait for the first dev server to exit before starting the second.

- [ ] **Step 6: Commit**

```bash
git add Makefile web package-lock.json
git commit -m "chore: add terminal workflow and web placeholder"
```

## Task 10: Phase 1 gate and handoff

**Files:**

- Modify: `README.md`

**Responsibilities:** Verify the repository is runnable, document the real contracts, and leave a clean starting point for an isolated Phase 2 session.

- [ ] **Step 1: Run the entire phase gate from a clean shell**

Run `uv sync --dev`, `uv run pytest backend/tests -q`, `npm --prefix web ci`, `npm --prefix web run lint`, and `npm --prefix web run build`. Expected: every command exits 0 and tests make no real LiteLLM calls.

- [ ] **Step 2: Verify secret and schema hygiene**

Run `git status --short --ignored`, `rg -n '(sk-|OPENAI_API_KEY=.+|ANTHROPIC_API_KEY=.+)' . --glob '!SPEC.md' --glob '!package-lock.json'`, and inspect `backend/evalbench/store.py`. Expected: `.env` and `evalbench.db` ignored, no secret-like values, and no suite-specific columns.

- [ ] **Step 3: Update README to match delivered Phase 1 behavior**

Document async SQLite setup, mocked-test guarantee, exact aggregate row shapes, CI formulas, and the private execution-context assumption. Keep future phases clearly labeled as not yet delivered.

- [ ] **Step 4: Final diff and commit**

Run `git diff --check` and `git status --short`. Expected: no whitespace errors and only intended README changes. Then:

```bash
git add README.md
git commit -m "docs: document evalbench core contracts"
```

- [ ] **Step 5: Stop and clear context**

Do not begin structured suite work in this session. Record the exact passing commands and final commit hash, then use `/clear` and open `docs/superpowers/plans/2026-07-15-evalbench-phase-2-structured.md`.
