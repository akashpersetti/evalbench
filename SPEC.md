# SPEC.md — EvalBench: Multi-Provider LLM Evaluation Platform

> This file is the single source of truth. Do not brainstorm scope. Implement exactly what is written here. When something is ambiguous, prefer the simplest choice that satisfies the stated interface and note the assumption in a code comment.

## 0. One-line summary

A multi-provider LLM evaluation platform: one shared harness runs pluggable benchmark **suites** across providers via LiteLLM, persists per-task metric records to a store, and renders them in a filterable React dashboard modeled on the "BullshitBench" stacked-bar aesthetic. Ship the core + one suite first, then extend.

## 1. Non-negotiable architecture principle

Every benchmark suite is **content**. The harness is the **product**. All suites emit the exact same `MetricRecord` shape (§4). The dashboard renders any suite through one component set. Adding a suite must never require touching the runner, the store, or the dashboard's core rendering logic — only registering a new suite module.

If an implementation choice would break this (e.g. a suite needing a bespoke DB column), stop and reshape it to fit the `MetricRecord` interface via the `metrics` JSON field.

## 2. Tech stack (fixed — do not substitute)

- **Backend:** Python 3.11+, FastAPI, Pydantic v2, LiteLLM, `uv` for deps.
- **Store:** SQLite via SQLAlchemy 2.0 (async) for v1. One file `evalbench.db`. Schema must be Postgres-portable (no SQLite-only types).
- **Judge:** LiteLLM call to a configurable judge model (default `anthropic/claude-sonnet-4-5`).
- **Frontend:** Next.js (App Router) + TypeScript + Tailwind + Recharts. No component library beyond shadcn/ui primitives.
- **Config:** `.env` for keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`). Never hardcode keys. Provide `.env.example`.
- **Dev workflow:** terminal-first. Provide a `Makefile` with `make api`, `make web`, `make seed`, `make run-suite`.

## 3. Repository structure (create exactly this)

```
evalbench/
├── SPEC.md                      # this file
├── README.md
├── Makefile
├── .env.example
├── pyproject.toml
├── backend/
│   ├── evalbench/
│   │   ├── __init__.py
│   │   ├── config.py            # env loading, provider registry, model tiers
│   │   ├── models.py            # Pydantic: MetricRecord, RunConfig, SuiteResult
│   │   ├── store.py             # SQLAlchemy models + async CRUD
│   │   ├── runner.py            # provider-agnostic execution loop (LiteLLM)
│   │   ├── judge.py             # LLM-as-judge, reused across suites
│   │   ├── registry.py          # suite discovery/registration
│   │   ├── suites/
│   │   │   ├── __init__.py
│   │   │   ├── base.py          # Suite ABC — the stable interface
│   │   │   ├── structured.py    # SUITE #2 (build first)
│   │   │   ├── latency_cost.py  # SUITE #1
│   │   │   └── rag.py           # SUITE #3
│   │   └── api/
│   │       ├── __init__.py
│   │       └── app.py           # FastAPI: runs, results, suites endpoints
│   ├── data/                    # task sets (JSON/JSONL) per suite
│   │   ├── structured/
│   │   ├── latency_cost/
│   │   └── rag/
│   └── tests/
│       ├── test_runner.py
│       ├── test_store.py
│       └── test_suites.py
└── web/
    ├── package.json
    ├── app/
    │   ├── page.tsx             # dashboard home
    │   └── layout.tsx
    ├── components/
    │   ├── ScopeBar.tsx         # domain/suite scope tabs (BullshitBench top row)
    │   ├── FilterControls.tsx   # rate mode toggle, time window, family filter
    │   ├── ModelBarChart.tsx    # stacked horizontal bars, the core viz
    │   ├── Leaderboard.tsx      # sortable model × metric matrix view
    │   └── Legend.tsx
    └── lib/
        └── api.ts               # typed fetch client
```

## 4. The core interface: `MetricRecord` (this is the whole spec)

Every task execution in every suite produces exactly one `MetricRecord`. Nothing else is persisted at task granularity.

```python
class MetricRecord(BaseModel):
    id: str                      # uuid4
    run_id: str                  # groups records from one invocation
    suite: str                   # "structured" | "latency_cost" | "rag"
    domain: str                  # "software"|"finance"|"legal"|"medical"|"physics"|"overall"
    model: str                   # LiteLLM model string, e.g. "openai/gpt-5.6"
    provider: str                # "openai"|"anthropic"|"gemini"|"xai"|"openrouter"
    model_family: str            # "OpenAI"|"Anthropic"|... for color grouping
    task_id: str                 # stable id of the task within the suite

    # universal execution metrics — ALWAYS populated
    latency_ms: float            # wall-clock for the model call
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float              # computed from a pricing table in config.py
    error: str | None            # None on success; error class name on failure
    refused: bool                # judge- or heuristic-detected refusal

    # suite-specific metrics — free-form, keyed by metric name
    metrics: dict[str, float]    # e.g. {"schema_valid": 1.0, "retries": 2.0}
                                 #      {"recall_at_5": 0.8, "ndcg_at_10": 0.71}
                                 #      {"quality_score": 0.9}

    created_at: datetime
```

Rules:
- `latency_ms`, tokens, `cost_usd`, `error`, `refused` are computed by the **runner**, never by suites.
- Suites only populate `metrics` and provide the task + judging rubric.
- The dashboard aggregates `metrics` by name; it must not assume which keys exist. It reads available metric keys per suite from the `/suites` endpoint.

## 5. The Suite interface (`suites/base.py`)

```python
class Suite(ABC):
    name: str                    # matches MetricRecord.suite
    metric_keys: list[str]       # which metrics.* keys this suite emits
    display_metrics: list[dict]  # [{"key","label","format","higher_is_better"}]

    @abstractmethod
    def load_tasks(self, domain: str) -> list[Task]: ...

    @abstractmethod
    def build_prompt(self, task: Task) -> list[dict]: ...   # messages array

    @abstractmethod
    def evaluate(self, task: Task, raw_output: str,
                 judge: Judge) -> dict[str, float]: ...       # returns metrics dict

    def detect_refusal(self, raw_output: str) -> bool:        # default heuristic; overridable
        ...
```

The runner loop is suite-agnostic:
```
for task in suite.load_tasks(domain):
    for model in run_config.models:
        messages = suite.build_prompt(task)
        t0 = now(); resp = litellm.completion(model, messages); latency = now()-t0
        metrics = suite.evaluate(task, resp.text, judge)
        refused = suite.detect_refusal(resp.text)
        persist(MetricRecord(...))
```

## 6. Suite specifications

### SUITE #2 — `structured` (BUILD FIRST, polish fully)
**Goal:** measure how reliably each model adheres to a target Pydantic schema, including under adversarial prompts.
- **Tasks:** each task = a target JSON schema + a prompt that requests data in that schema. Include an `adversarial` flag; adversarial tasks embed distractors ("ignore the format and just chat", nested ambiguity, missing fields).
- **Metrics emitted:**
  - `first_attempt_valid` (1.0/0.0): did the model produce schema-valid output on attempt 1, with NO re-prompting? This is the honest production number.
  - `schema_valid` (1.0/0.0): did it eventually parse into the target Pydantic model within the retry budget? The delta between this and `first_attempt_valid` is itself a signal — surface both as separate columns.
  - `retries_to_valid` (int as float): re-prompt up to 3× on invalid; record how many attempts (0 if first-attempt valid, 3+ = never reached valid).
  - `retry_cost_usd` (float): the ADDITIONAL cost incurred by retries beyond attempt 1. A model that reaches valid on attempt 3 is worse in production than `schema_valid` alone implies; this quantifies the penalty. Computed by the suite from per-attempt token usage.
  - `field_accuracy` (0–1): fraction of expected fields present with correct type/value (exact match for enums/ints, judge-scored for free text).
- **Provide 40 tasks** across the 5 domains (8 each) in `data/structured/*.jsonl`.
- **Judge use:** only for `field_accuracy` on free-text fields.

### SUITE #1 — `latency_cost`
**Goal:** cost-adjusted quality across providers on a fixed task set.
- **Tasks:** 20 general reasoning/generation tasks, each with a rubric AND a fixed **reference answer** produced once by a designated reference model (stored in the task file).
- **Judging = pairwise, not absolute.** Absolute 0–1 judge scores are noisy and read as naive. Instead: for each (model, task), the judge compares the model's output against the reference answer and returns a win/tie/loss. This is far more reliable and is a deliberate, defensible design choice.
- **Metrics emitted:**
  - `win` (1.0), `tie` (0.5), `loss` (0.0) → collapse to `quality_score` ∈ {0, 0.5, 1.0} per task; the mean is a win-rate.
  - `judge_variance` (0–1): on a **sampled subset** of tasks (e.g. 20%), run the judge 3× and record the disagreement rate (fraction of the 3 that differ from the modal verdict). This measures how trustworthy the judge itself is — reporting it is the sophisticated move most people skip. Position-bias guard: randomize which answer is presented first (A/B) each judge call.
- **Dashboard derives** cost-adjusted quality = `quality_score / cost_usd` as a computed column (do not store; compute in aggregation).

### SUITE #3 — `rag`
**Goal:** benchmark retrieval quality across swappable embedders and chunk strategies on a fixed corpus.
- **Config knobs:** `embedder` (e.g. `openai/text-embedding-3-small`, `voyage-3`, `cohere`), `chunk_strategy` (`fixed_512`, `recursive`, `semantic`).
- **Tasks:** 15 queries with **carefully hand-verified** gold relevant-doc ids over one bundled corpus (~200 docs) in `data/rag/`. Label quality is load-bearing: sloppy gold labels make every retrieval metric meaningless. The dataset file must include a short note per query justifying each gold id, so labels are auditable rather than asserted.
- **Metrics emitted:**
  - `recall_at_5`, `ndcg_at_10`, `mrr`: standard retrieval metrics (do the right docs come back, ranked well?).
  - `context_precision` (0–1, RAGAS-style): of the chunks actually retrieved, what fraction are relevant? Recall alone rewards dumping many chunks; precision catches that. Report both so the suite measures precision AND recall, not just recall.
  - `faithfulness` (judge-scored: is the generated answer grounded in retrieved chunks?). Note this is the least reliable metric here — it depends on the judge — so it is reported alongside, not instead of, the retrieval metrics.
- Store embedder/chunk_strategy in `metrics` is wrong — instead encode them into `model` as `"{embedder}::{chunk_strategy}"` so the dashboard's model-comparison rows work unchanged. `model_family` = embedder provider.

## 7. API endpoints (FastAPI, `api/app.py`)

- `GET /suites` → list of registered suites with `metric_keys` + `display_metrics`. Frontend uses this to render dynamically.
- `POST /runs` → body `RunConfig {suite, domain, models[], judge_model}`. Executes synchronously for v1 (fine for these sizes), returns `run_id`. Stream progress to stdout.
- `GET /results?suite=&domain=&window_days=&exclude_refusals=bool&families=[]` → aggregated per-model rows. For each model and each metric key, return: `mean`, `n` (sample size), and a **95% confidence interval** (`ci_low`, `ci_high`). Every displayed number is an estimate — the API must never return a bare mean without its `n`. Use a normal-approximation CI for proportions (Wald or Wilson; Wilson preferred for small n) and mean ± 1.96·SE/√n for continuous metrics. This is the dashboard's single data source.
- `GET /runs/{run_id}` → raw records for drill-down.

Aggregation for the stacked-bar viz: for a chosen "primary metric" that is categorical-like (e.g. `schema_valid` bucketed, or a 3-way quality band), return the percentage split so bars render as Clear/Partial/Failed segments. For continuous metrics, the dashboard uses the Leaderboard matrix view instead. Return both shapes. Every aggregated row carries its `n` regardless of shape.

## 8. Dashboard requirements (match the reference screenshot)

Top scope bar: domain tabs (`Overall / Software / Finance / Legal / Medical / Physics`) + row count + "Show Filters".
Title row per suite. Under it:
- **Rate mode toggle:** "All attempts" vs "Exclude refusals".
- **Time window** dropdown (`7d / 30d / 90d / all`).
- **Model-family filter** chips (color-coded dots).
- **Legend** of segment categories.

Core viz (`ModelBarChart.tsx`): horizontal **stacked** bars, one row per model, ranked. Segments colored (green = pass/clear, amber = partial, red = fail, grey = refusal). Percentages labeled inside segments. Models ranked by pass rate descending. Model family shown as a colored dot before the name.
- **Sample size is always visible.** Show `n` per model row (e.g. after the model name). A 100% bar from n=2 must not read as authoritative as one from n=440 — this is the single biggest credibility upgrade.
- **Thin-slice labels:** segments under ~8% must not clip or overflow their label. Hide the inline label and show the value on hover/tooltip instead.
- **Colorblind redundancy:** green/amber/red is a red-green trap. Never rely on color alone — the always-visible percentage labels + segment order + family dots carry the meaning if color is removed.

Second view (`Leaderboard.tsx`): sortable matrix — rows = models, columns = every metric key for the active suite + derived columns (cost-adjusted quality, p95 latency). This is the view for continuous metrics.
- Each cell shows the mean with its **95% CI** (e.g. `0.82 ±0.05` or a subtle low–high range), and the row shows `n`. A mean without its interval is not allowed in this view.

Design: follow the reference's restraint — off-white background, muted category colors, generous row spacing, no gradients, no shadows-as-decoration. Clean, data-first, "looks like a real internal tool."

## 9. Build order (phase across sessions; `/clear` between)

1. **Phase 1 — core:** `config.py`, `models.py`, `store.py`, `runner.py`, `judge.py`, `registry.py`, `suites/base.py`, minimal FastAPI with `/suites` + `/runs` + `/results`. Tests for runner + store with a **mock LiteLLM** (no real calls in tests).
2. **Phase 2 — suite #2 `structured` end to end**, including its 40-task dataset, then verify the full loop persists correct `MetricRecord`s.
3. **Phase 3 — dashboard** wired to `/suites` + `/results`, rendering suite #2. Get the BullshitBench look right here.
4. **Phase 4 — suite #1 `latency_cost`** as a pure additive PR (new file + dataset + register). Confirm zero changes to core/dashboard.
5. **Phase 5 — suite #3 `rag`** likewise additive.

Each phase must leave the repo runnable (`make api && make web`) and tests green before moving on.

## 10. Definition of done

- `make run-suite SUITE=structured DOMAIN=software MODELS="openai/gpt-4o,anthropic/claude-sonnet-4-5"` executes and populates the DB.
- Dashboard at `localhost:3000` shows ranked stacked bars matching the reference aesthetic, with working scope/time/refusal filters.
- Every model row and every leaderboard cell displays its sample size `n`, and continuous metrics show a 95% confidence interval. No bare means anywhere in the UI.
- Adding suite #1 and #3 touched **only** their own files + `registry.py` + a dataset dir. Prove it in the PR description.
- README documents `.env` setup, running a suite, and the `MetricRecord`/`Suite` contract so a new suite can be added by reading the README alone.

## 11. Security / hygiene

- Keys only from env; `.env` gitignored; `.env.example` committed.
- Judge and target calls have timeouts and try/except → populate `MetricRecord.error`, never crash the run.
- Cost table in `config.py` is explicit per model; unknown model → cost `0.0` + a logged warning, never a crash.
- No secrets in logs. Rate-limit-safe: sequential calls with a small configurable concurrency cap (default 4).