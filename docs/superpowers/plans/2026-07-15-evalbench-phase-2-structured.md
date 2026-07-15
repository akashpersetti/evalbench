# EvalBench Phase 2 — Structured Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add suite #2, `structured`, with 40 audited tasks, schema validation and retry accounting, judge-assisted free-text field accuracy, registry/API exposure, and an end-to-end persistence test.

**Architecture:** `StructuredSuite` is content plugged into the Phase 1 contracts. It parses the initial model text, validates against a dynamically generated Pydantic v2 model, and requests at most three corrective calls through `Task._execution_context`; the runner continues to own universal metrics and persistence. Only free-text field comparisons use the shared judge.

**Tech Stack:** Existing Phase 1 Python stack; Pydantic v2 dynamic models; JSONL datasets; pytest with injected fake LiteLLM and fake judge.

## Global Constraints

- Begin only from a green Phase 1 checkout; do not modify the MetricRecord, Suite, store schema, aggregation, or dashboard contracts.
- Create exactly 40 tasks: 8 each in software, finance, legal, medical, and physics.
- `first_attempt_valid`, `schema_valid`, `retries_to_valid`, `retry_cost_usd`, and `field_accuracy` are the only emitted metric keys.
- “Re-prompt up to 3×” means one initial call plus at most three retry calls. `retries_to_valid` is `0`, `1`, `2`, or `3`; when all four attempts fail, it is `3` and `schema_valid=0` distinguishes failure from success on the final retry.
- The runner sums all provider attempts into universal latency/tokens/cost. `retry_cost_usd` is only the additional cost of context completion calls after the initial call.
- No test may make a real target or judge API call.
- Phase gate: `make run-suite` persists valid exact-shape records under a fake integration test, `/suites` exposes metadata, all tests/builds pass, and `make api`/`make web` start before `/clear`.
- `[STRONGER MODEL REVIEW]` marks test/data quality work that should not be delegated to a cheap executor without review.

---

## Locked structured task and suite shape

Each JSONL line has exactly this shape:

```json
{
  "id": "software-01",
  "domain": "software",
  "prompt": "...",
  "schema": {
    "title": "Software01Output",
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": false
  },
  "expected": {},
  "free_text_fields": ["/summary"],
  "adversarial": false
}
```

All schemas must be expressible by the deliberately small converter below: object, array, string, integer, number, boolean, nullable via `anyOf` with `null`, and string `enum`. Do not add general JSON Schema support.

```python
class StructuredSuite(Suite):
    name = "structured"
    metric_keys = [
        "first_attempt_valid", "schema_valid", "retries_to_valid",
        "retry_cost_usd", "field_accuracy",
    ]
    display_metrics = [
        {"key":"schema_valid", "label":"Schema valid", "format":"percent", "higher_is_better":True},
        {"key":"first_attempt_valid", "label":"First-attempt valid", "format":"percent", "higher_is_better":True},
        {"key":"field_accuracy", "label":"Field accuracy", "format":"percent", "higher_is_better":True},
        {"key":"retries_to_valid", "label":"Retries to valid", "format":"number", "higher_is_better":False},
        {"key":"retry_cost_usd", "label":"Retry cost", "format":"currency", "higher_is_better":False},
    ]

    def __init__(self, data_dir: Path | None = None): ...
    def load_tasks(self, domain: str) -> list[Task]: ...
    def build_prompt(self, task: Task) -> list[dict]: ...
    def evaluate(self, task: Task, raw_output: str, judge: Judge) -> dict[str, float]: ...
```

## Task 1: Pydantic schema conversion and output parsing

**Files:**

- Create: `backend/evalbench/suites/structured.py`
- Modify: `backend/tests/test_suites.py`

**Responsibilities:** Convert the supported dataset schema subset into strict Pydantic models and extract exactly one JSON value from plain or fenced model output.

**Interfaces:**

```python
def annotation_from_schema(name: str, schema: dict[str, Any]) -> Any: ...
def model_from_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]: ...
def extract_json(raw_output: str) -> Any: ...
def validate_output(raw_output: str, schema: dict[str, Any]) -> tuple[Any | None, bool, str | None]: ...
```

- [ ] **Step 1: Write failing parser/converter tests** `[STRONGER MODEL REVIEW: Phase 1 test-design carryover]`

Cover nested required objects, lists, enum literals, strict integers rejecting numeric strings, nullable fields, absent required fields, forbidden extra fields, plain JSON, a single ```json fence, leading/trailing prose with one balanced JSON object, braces inside quoted strings, malformed JSON, and multiple top-level JSON values rejected as ambiguous.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`. Expected: `structured.py` import/function failures.

- [ ] **Step 3: Implement the supported annotation converter**

Use `pydantic.create_model`, `ConfigDict(strict=True, extra="forbid")`, `Literal[tuple(enum_values)]`, `list[item_annotation]`, and `T | None` for the one supported nullable form. Required fields use `...`; optional fields default to `None` only if their schema permits null, otherwise omit optional non-null fields with a typed default of `None` only when the dataset explicitly gives a default. Raise `ValueError` naming the schema path for unsupported constructs so bad dataset content fails at load time.

- [ ] **Step 4: Implement deterministic JSON extraction**

Strip one outer markdown fence. Otherwise scan from the first `{` or `[` with a quote/escape-aware depth counter to the matching close. Reject non-whitespace after the extracted value except ordinary prose with no second JSON opener; reject a second complete JSON value. Parse using `json.loads`. `validate_output` returns `(parsed_value, True, None)` after successful Pydantic validation, `(parsed_value, False, concise_error_string)` when JSON parses but schema validation fails, or `(None, False, concise_error_string)` when JSON extraction/parsing fails. Preserve the original parsed value rather than replacing it with `model_dump()` so strict type/value field accuracy sees exactly what the model emitted. This helper never calls a judge.

- [ ] **Step 5: Run focused tests and commit**

Run `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`. Expected: all focused tests pass.

```bash
git add backend/evalbench/suites/structured.py backend/tests/test_suites.py
git commit -m "feat: validate structured outputs with pydantic"
```

## Task 2: Structured prompts, retries, and metric evaluation

**Files:**

- Modify: `backend/evalbench/suites/structured.py`
- Modify: `backend/tests/test_suites.py`

**Responsibilities:** Build schema-constrained messages, run the fixed retry policy through the generic context, calculate the five declared metrics, and use the judge only for declared free-text fields.

**Interfaces:**

```python
def build_retry_messages(original_messages: list[dict], invalid_output: str,
                         validation_error: str, schema: dict[str, Any]) -> list[dict]: ...
def iter_expected_leaves(expected: Any, pointer: str = "") -> list[tuple[str, Any]]: ...
def field_accuracy(task: Task, parsed: Any | None, judge: Judge) -> float: ...
```

- [ ] **Step 1: Write failing metric tests** `[STRONGER MODEL REVIEW: retry test design]`

Use a fake execution context whose `complete()` returns queued `CallResult`s and appends them to `.calls`, plus a fake judge recording invocations. Test:

- valid first output: both validity metrics `1`, retries `0`, retry cost `0`;
- invalid then valid: first `0`, eventual `1`, retries `1`, retry cost equals only call index 1;
- valid on fourth total attempt: retries `3` and eventual `1`;
- all four syntactically invalid: retries `3`, eventual `0`, field accuracy `0`;
- schema-invalid but parseable JSON: eventual `0` while correct present leaves still receive partial field-accuracy credit;
- a refusal remains evaluation output; refusal classification is still runner-owned;
- exact enum/int/boolean/nested/list leaves score `1` only on exact type/value;
- absent/wrong leaves score `0`;
- free-text pointers call `judge.score_free_text` and use its returned score;
- no free-text pointer causes zero judge calls;
- returned keys are exactly the five declared metrics and all values are floats.

- [ ] **Step 2: Run and confirm failures**

Run `uv run pytest backend/tests/test_suites.py -k 'structured and (retry or accuracy or metrics)' -q`. Expected: missing behavior.

- [ ] **Step 3: Implement prompt construction**

System message: return only JSON conforming to the supplied schema, with no markdown. User message contains the task prompt followed by canonical compact JSON schema (`sort_keys=True`). Retry messages preserve the original messages, append the invalid assistant output, then a user correction containing the concise validation error and same schema. Do not reveal expected values in any prompt.

- [ ] **Step 4: Implement retry evaluation**

Validate `raw_output`; save `first_attempt_valid` immediately. While invalid and retries `< 3`, require `task._execution_context`, call `complete(build_retry_messages(...))`, and revalidate. Keep the most recent parseable JSON value even if no attempt becomes schema-valid; use it for partial field accuracy. Calculate retry cost from the `CallResult.cost_usd` objects returned by retry calls, not by re-reading total context state. Do not catch provider exceptions; the runner records their class and preserves metered earlier calls.

- [ ] **Step 5: Implement field accuracy**

Flatten `expected` into RFC 6901-style leaf pointers, escaping `~` and `/`. Each leaf contributes equal weight. For non-free-text leaves, require matching Python type before exact equality (`bool` must not pass as `int`). For a free-text leaf, missing/wrong-type contributes zero; otherwise call `judge.score_free_text(prompt=task.prompt, expected=str(expected), actual=str(actual), rubric="Semantic equivalence and factual completeness; ignore wording differences.")`. Return arithmetic mean, or `1.0` for a deliberately empty expected object.

- [ ] **Step 6: Run focused tests and commit**

Run `uv run pytest backend/tests/test_suites.py -k structured -q`. Expected: all structured unit tests pass with fakes.

```bash
git add backend/evalbench/suites/structured.py backend/tests/test_suites.py
git commit -m "feat: evaluate structured adherence and retries"
```

## Task 3: Build and audit the 40-task dataset

**Files:**

- Create: `backend/data/structured/software.jsonl`
- Create: `backend/data/structured/finance.jsonl`
- Create: `backend/data/structured/legal.jsonl`
- Create: `backend/data/structured/medical.jsonl`
- Create: `backend/data/structured/physics.jsonl`
- Modify: `backend/evalbench/suites/structured.py`
- Modify: `backend/tests/test_suites.py`

**Responsibilities:** Supply balanced, deterministic task content and load it into generic `Task` instances without adding code per task.

**Interfaces:** `StructuredSuite.load_tasks("software")` returns the 8 software tasks; `load_tasks("overall")` returns all 40 sorted by `(domain,id)`; every task payload contains `schema`, `expected`, `free_text_fields`, and `adversarial`.

- [ ] **Step 1: Write failing dataset validation tests** `[STRONGER MODEL REVIEW: dataset and adversarial-task quality]`

Assert exactly five files/eight nonblank lines each; globally unique IDs matching `<domain>-NN`; line domain matches filename; exactly four adversarial tasks per domain; schemas convert successfully; expected values validate against their schemas; every free-text pointer resolves in expected; prompts do not contain the serialized expected answer; and both per-domain/overall loaders return deterministic counts/order.

- [ ] **Step 2: Run and confirm failure**

Run `uv run pytest backend/tests/test_suites.py -k 'structured and dataset' -q`. Expected: missing dataset files.

- [ ] **Step 3: Author eight software tasks** `[STRONGER MODEL REQUIRED]`

Use IDs `software-01`…`08` and these distinct shapes in order: API error object, dependency manifest, code-review findings list, deployment plan, typed function signature, test-case matrix, incident summary, and nested service topology. Tasks 02, 04, 06, 08 are adversarial using respectively “just chat” distraction, conflicting nested format, omitted-field temptation, and an instruction embedded in quoted source text. Include at most two free-text leaves per task.

- [ ] **Step 4: Author eight finance tasks** `[STRONGER MODEL REQUIRED]`

Use IDs `finance-01`…`08`: trade ticket, quarterly metrics, loan terms, risk limits, portfolio allocation, invoice extraction, cash-flow schedule, compliance alert. Tasks 02, 04, 06, 08 are adversarial. Use unambiguous decimal units in prompts/expected values; never rely on floating-point arithmetic by the model for the expected answer.

- [ ] **Step 5: Author eight legal tasks** `[STRONGER MODEL REQUIRED]`

Use IDs `legal-01`…`08`: clause classification, filing metadata, obligations list, citation extraction, party/role map, deadline schedule, issue checklist, redaction manifest. Tasks 02, 04, 06, 08 are adversarial. State these are synthetic informational extraction tasks, not legal advice.

- [ ] **Step 6: Author eight medical tasks** `[STRONGER MODEL REQUIRED]`

Use IDs `medical-01`…`08`: synthetic lab panel, medication schedule, symptom timeline, coding extraction, contraindication list, trial eligibility, vitals record, referral summary. Tasks 02, 04, 06, 08 are adversarial. Use fictional data, no patient identifiers, and no diagnosis/treatment generation.

- [ ] **Step 7: Author eight physics tasks** `[STRONGER MODEL REQUIRED]`

Use IDs `physics-01`…`08`: constants table, experiment setup, measurement series, particle classification, unit conversion record, circuit description, orbital parameters, uncertainty report. Tasks 02, 04, 06, 08 are adversarial. Put units in explicit fields and exact expected values in the prompt so evaluation is deterministic.

- [ ] **Step 8: Implement loader and run dataset tests**

Default data root is `Path(__file__).resolve().parents[2] / "data" / "structured"`. Parse each nonblank line with line-numbered `ValueError`s. Reject unknown domains before touching files. Convert rows to `Task(id=..., domain=..., prompt=..., payload={...})`; leave `requires_generation=True`.

Run `uv run pytest backend/tests/test_suites.py -k 'structured and dataset' -q`. Expected: exact count/distribution/audit tests pass.

- [ ] **Step 9: Commit**

```bash
git add backend/data/structured backend/evalbench/suites/structured.py backend/tests/test_suites.py
git commit -m "data: add structured benchmark tasks"
```

## Task 4: Register suite and verify full persisted MetricRecords

**Files:**

- Modify: `backend/evalbench/registry.py`
- Modify: `backend/tests/test_runner.py`
- Modify: `backend/tests/test_suites.py`

**Responsibilities:** Make structured discoverable and prove the complete API/runner/retry/store loop without a provider call.

- [ ] **Step 1: Register exactly one shared suite instance**

Add an explicit import of `StructuredSuite` and `register_suite(StructuredSuite())` in `registry.py`. Do not change registry functions or add dynamic discovery.

- [ ] **Step 2: Write the end-to-end fake test** `[STRONGER MODEL REVIEW: full-loop assertion quality]`

Use a temporary DB, a one-task temporary structured JSONL directory, injected fake target completion returning invalid JSON then valid JSON, and injected fake judge. Execute one model. Assert exactly one persisted record; IDs/run fields; suite/domain/model/provider/family/task; total latency/tokens/cost include both target attempts; `error is None`; refusal false; exact five metric keys; first-attempt `0`; eventual `1`; retries `1`; retry cost equals second call only; aware UTC timestamp; and returned `SuiteResult.records` equals DB raw run records.

Also call `/suites` and assert structured metadata exact order/content, then `/results` and assert both `metrics` matrix estimates and stacked entries for the binary keys include `n=1` and CIs.

- [ ] **Step 3: Run full backend tests**

Run `uv run pytest backend/tests -q`. Expected: all pass; generic registry contract now covers structured automatically; no real LiteLLM calls.

- [ ] **Step 4: Commit**

```bash
git add backend/evalbench/registry.py backend/tests
git commit -m "feat: register structured suite end to end"
```

## Task 5: Complete terminal workflow and documentation

**Files:**

- Modify: `Makefile`
- Modify: `README.md`

**Responsibilities:** Make the spec's structured run command real and document enough contract detail to add future suites without reading implementation code.

- [ ] **Step 1: Replace Phase 1 seed stub**

`make seed` invokes `make run-suite SUITE=structured DOMAIN=software` and accepts `MODELS` from the environment/Make command line; default `MODELS` to one explicitly priced inexpensive model documented in README. It may require a real provider key and must say so; tests never invoke it.

- [ ] **Step 2: Expand README**

Document `.env`, `uv sync`, `npm --prefix web install`, all make targets, the exact Definition-of-Done run command, the five metrics/retry interpretation, dataset layout, exact `MetricRecord`/`Suite` contracts, display metric format vocabulary (`percent`, `number`, `currency`), and explicit three-file extension rule (suite module, registry line, dataset directory). State universal totals include retries and `retry_cost_usd` is the retry-only subset.

- [ ] **Step 3: Run the Phase 2 gate**

Run `uv run pytest backend/tests -q`, `npm --prefix web run lint`, `npm --prefix web run build`, and `git diff --check`. Expected: all exit 0.

Start `make api`; `curl -fsS http://127.0.0.1:8000/suites` must contain `structured` and all five metric keys. Stop it. Start `make web`; curl port 3000 and confirm the placeholder still renders. Stop it.

- [ ] **Step 4: Verify no accidental schema/core expansion**

Inspect `git diff <phase-1-final-commit> -- backend/evalbench/store.py backend/evalbench/models.py backend/evalbench/suites/base.py`. Expected: no diff. Run `git status --short` and ensure `evalbench.db`/`.env` are not staged.

- [ ] **Step 5: Commit and clear**

```bash
git add Makefile README.md
git commit -m "docs: document structured evaluation workflow"
```

Record passing commands and commit hash. Do not start dashboard work. Use `/clear` and open `docs/superpowers/plans/2026-07-15-evalbench-phase-3-dashboard.md`.
