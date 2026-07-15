# EvalBench

## Purpose

EvalBench is a provider-agnostic LLM evaluation platform. One shared harness runs pluggable benchmark suites across providers through LiteLLM, persists per-task metric records, and exposes results for future dashboard use.

## Phase 1 status

Phase 1 establishes the backend project, dependency toolchain, core contracts, runner, store, judge, registry, and minimal API. This scaffolding task provides the project and dependency foundation only; the benchmark suites and dashboard are delivered in later phases.

### Dashboard

The dashboard is planned for Phase 3 delivery and is not part of this scaffold.

### `structured` suite

The `structured` suite is planned for Phase 2 delivery and is not part of this scaffold.

### `latency_cost` suite

The `latency_cost` suite is planned for later phase delivery and is not part of this scaffold.

### `rag` suite

The `rag` suite is planned for later phase delivery and is not part of this scaffold.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

## Environment setup

Copy the example environment file and add provider credentials as needed:

```bash
cp .env.example .env
```

Keys are read from `.env`; do not commit `.env` or database files.

## Commands

Install the project and development dependencies:

```bash
uv sync --dev
```

Run the backend tests:

```bash
uv run pytest backend/tests -q
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

## Adding a suite

1. Add the suite module under `backend/evalbench/suites/`.
2. Register one suite instance in the suite registry.
3. Add the suite dataset in its dataset directory.

The runner, store, and dashboard core should not need suite-specific changes.
