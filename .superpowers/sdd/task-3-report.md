# Task 3 Report: Structured Dataset

## Status

Implemented and audited the 40-task structured dataset and its generic loader.
The implementation commit is `1e59cdd` (`data: add structured benchmark tasks`).

## Dataset counts and adversarial split

| Domain | Tasks | Adversarial | IDs |
| --- | ---: | ---: | --- |
| software | 8 | 4 | `software-01` through `software-08` |
| finance | 8 | 4 | `finance-01` through `finance-08` |
| legal | 8 | 4 | `legal-01` through `legal-08` |
| medical | 8 | 4 | `medical-01` through `medical-08` |
| physics | 8 | 4 | `physics-01` through `physics-08` |
| **Overall** | **40** | **20** | Globally unique and deterministic |

Within every domain, tasks 02, 04, 06, and 08 are adversarial. Overall loading
is sorted by `(domain, id)`; per-domain loading preserves the deterministic ID
order.

## Audit method

- Added dataset-first tests covering exactly five files, eight nonblank rows per
  file, exact IDs and domains, global uniqueness, and the 4/8 adversarial split.
- Converted every row schema through `model_from_schema` and strictly validated
  every expected value, checking that the resulting model dump preserves exact
  shape and values.
- Flattened expected values into pointers and checked every free-text pointer,
  with a maximum of two pointers per task.
- Checked that prompts do not contain compact, sorted, or default serialized
  expected JSON and rejected credential-like prompt patterns.
- Checked loader determinism, exact counts, default `requires_generation=True`,
  exact payload keys, unknown-domain rejection before file access, and
  filename/line-numbered JSON errors.
- Manually reviewed each domain’s required shape order and adversarial pattern;
  all content is synthetic and contains no patient identifiers, credentials,
  or real-world legal/medical advice generation.

## RED/GREEN evidence

Required RED command before implementation:

```text
uv run pytest backend/tests/test_suites.py -k 'structured and dataset' -q
6 failed, 93 deselected
```

The failures were the intended missing legal/medical/physics files, empty
loader, absent unknown-domain error, and absent line-numbered parse error.

Focused GREEN result:

```text
uv run pytest backend/tests/test_suites.py -k 'structured and dataset' -q
6 passed, 93 deselected
```

## Files

- `backend/data/structured/software.jsonl`
- `backend/data/structured/finance.jsonl`
- `backend/data/structured/legal.jsonl`
- `backend/data/structured/medical.jsonl`
- `backend/data/structured/physics.jsonl`
- `backend/evalbench/suites/structured.py`
- `backend/tests/test_suites.py`

The unrelated existing change in
`docs/superpowers/plans/2026-07-15-evalbench-subagent-controller-prompts.md`
was preserved and not staged.

## Tests and runtime checks

- `uv run pytest backend/tests/test_suites.py -k 'structured and dataset' -q`:
  6 passed, 93 deselected.
- `uv run pytest backend/tests -q`: 202 passed, 1 skipped.
- Direct dataset audit: 40 rows, 8 per domain, 4 adversarial per domain;
  schemas, expected values, pointers, and serialized-answer leakage checks
  passed.
- `make api`: Uvicorn started on port 8000; `GET /suites` returned HTTP 200.
- `make web`: Next.js reported ready on port 3000; a local HEAD request returned
  HTTP 200.
- No target or judge API calls were made.

## Self-review

- Loader default root is `backend/data/structured` derived from the suite file.
- Unknown domains are rejected before any file access.
- Nonblank JSONL rows receive filename and line-numbered errors.
- Rows are converted to generic `Task` instances without per-task code and keep
  `requires_generation=True`.
- No changes were made to `MetricRecord`, `Suite`/base, runner, store,
  aggregation, dashboard, registry, API, Makefile, README, or other suites.
- The implementation emits no additional structured metric keys; the existing
  suite metric contract remains unchanged.

## Concerns

The current Phase 1 registry intentionally has no registered suites, so the
running `/suites` endpoint returns `[]` even though the endpoint itself starts
and responds successfully. Existing tests explicitly lock that Phase 1
behavior, and registering `StructuredSuite` would violate this task’s scope
prohibition on registry/API changes. This remains for the later registration
phase.

## Commit

- `1e59cdd data: add structured benchmark tasks`

## Task 3 review-fix evidence

### Finding 1: numeric schema fields and float expected values

- Changed paths: `backend/data/structured/medical.jsonl`,
  `backend/data/structured/physics.jsonl`,
  `backend/evalbench/suites/structured.py`, and
  `backend/tests/test_suites.py`.
- Updated every dataset `number` prompt cue that used integer-looking
  spellings, including `140.0`, `92.0`, `299792458.0`, `220.0`, `7000000.0`,
  and `5820.0`.
- Added an all-dataset numeric audit covering exact `float` expected values and
  decimal prompt cues, plus loader validation that rejects integer expected
  values for `number` schemas before field-accuracy scoring can misclassify
  them. Integer schema fields are also checked for exact `int` values.

### Finding 2: legal-06 defaulted fields were not explicit in the prompt

- Changed paths: `backend/data/structured/legal.jsonl` and
  `backend/tests/test_suites.py`.
- legal-06 now explicitly requests `time` and `days_after_service` for both
  deadline entries, including the default values `unspecified` and `0`.
- Added a focused prompt regression asserting all four defaulted-field cues.

### Finding 3: loaded IDs accepted any suffix after the domain prefix

- Changed paths: `backend/evalbench/suites/structured.py` and
  `backend/tests/test_suites.py`.
- Loader validation now requires the exact format `<domain>-NN` and retains
  filename/line-numbered error wrapping.
- Added a focused `software-1` malformed-ID regression.

### Changed paths

- `backend/data/structured/legal.jsonl`
- `backend/data/structured/medical.jsonl`
- `backend/data/structured/physics.jsonl`
- `backend/evalbench/suites/structured.py`
- `backend/tests/test_suites.py`
- `.superpowers/sdd/task-3-report.md`

### Verification commands and passing outputs

```text
uv run pytest backend/tests/test_suites.py -k 'structured and dataset' -q
9 passed, 94 deselected in 1.99s

uv run pytest backend/tests -q
206 passed, 1 skipped in 4.33s
```

No target or judge calls were made.
