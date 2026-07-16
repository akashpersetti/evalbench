# Task 1 Report: Pydantic schema conversion and output parsing

## Status

Implemented and committed Task 1 only. No registry, runner, metrics, dataset, store, aggregation, dashboard, or other-suite contract was modified.

## Implementation

- Added the strict supported-schema converter in `backend/evalbench/suites/structured.py`.
- Dynamic models use Pydantic v2 `create_model` with `ConfigDict(strict=True, extra="forbid")`.
- Supported values are object, array, string, integer, number, boolean, string enum literals, and one nullable `anyOf` form containing a null branch.
- Unsupported schema content fails with a `ValueError` containing its schema path.
- Added deterministic extraction of one plain or fenced JSON object/array, including quote/escape-aware nesting and ambiguity detection.
- `validate_output` preserves the parsed source value, returns concise validation errors, and does not invoke a judge.

## Files

- Created: `backend/evalbench/suites/structured.py`
- Modified: `backend/tests/test_suites.py`

## RED evidence

- `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`: failed during collection because `evalbench.suites.structured` did not exist.
- Post-GREEN review test for a trailing scalar JSON value: failed because the extractor accepted it.
- Post-GREEN review test for a trailing second JSON opener: failed because the extractor accepted it.

## GREEN evidence and tests

- Focused Task 1 command: `13 passed, 58 deselected`.
- Full relevant suite: `174 passed, 1 skipped` from `uv run pytest backend/tests -q`.
- Scoped whitespace check: `git diff --check -- backend/evalbench/suites/structured.py backend/tests/test_suites.py` passed.

## Self-review

- Verified nested required objects, arrays, enums, strict integer handling, nullable values, missing required fields, extra-field rejection, malformed input, prose/fence extraction, quote/brace handling, and multiple-value ambiguity.
- Added the two ambiguity regression tests discovered during review and reran both focused and full tests after each fix.
- Confirmed generated models preserve strict validation and `validate_output` never replaces parsed output with `model_dump()`.

## Concerns

- Repository-wide `git diff --check` remains nonzero because of a pre-existing missing final newline in the unrelated modified `docs/superpowers/plans/2026-07-15-evalbench-subagent-controller-prompts.md`; the scoped Task 1 check is clean.
- The remaining Phase 2 suite behavior (retries, metrics, datasets, registration, and integration) is intentionally out of scope for this Task 1 commit.

## Commit

- `4d9e5d56e05b76162179a37a4b6384ea8a57e077` — `feat: validate structured outputs with pydantic`

## Post-review covering-test evidence

- After both review fixes: `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q` reported `13 passed, 58 deselected`.
- After both review fixes: `uv run pytest backend/tests -q` reported `174 passed, 1 skipped`.

## Task 1 review-fix evidence

- Finding addressed: `backend/tests/test_suites.py` lacked a negative regression proving the string enum rejects an unlisted value such as `status="archived"`.
- Changed path: `backend/tests/test_suites.py`
- Exact command: `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`
- Passing output: `14 passed, 58 deselected in 1.85s`

## Task 1 scalar-ambiguity fix evidence

- Finding addressed: `extract_json` accepted multiple trailing scalar JSON values as prose, including `{"answer": 1}\n42 43` and `{"answer": true}\ntrue false`.
- Changed files: `backend/evalbench/suites/structured.py`, `backend/tests/test_suites.py`
- Exact command: `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`
- Output: `16 passed, 58 deselected in 1.68s`
- Exact command: `uv run pytest backend/tests -q`
- Output: `177 passed, 1 skipped in 3.38s`

## Task 1 default-validation fix evidence

- Finding: schema-provided optional defaults were passed to `create_model` without strict validation, allowing an integer field with `default="wrong"` to produce a model instance containing a string.
- Changed paths:
  - `backend/evalbench/suites/structured.py`
  - `backend/tests/test_suites.py`
- Exact command: `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`
- Passing output: `18 passed, 58 deselected in 1.70s`
- Exact command: `uv run pytest backend/tests -q`
- Passing output: `179 passed, 1 skipped in 3.46s`

## Whole-phase final-fix evidence

- Finding 1 addressed: `json.loads` accepted non-standard `NaN`, `Infinity`, and `-Infinity` constants, allowing them to become schema-valid numbers. `_load_json` now rejects them through `parse_constant`.
- Finding 2 addressed: `extract_json` accepted unmatched trailing JSON closing delimiters such as `{"x": 1} ]` and `{"x": 1} }`. It now rejects unmatched trailing closers while retaining quote-aware ordinary prose extraction.
- Changed paths: `backend/evalbench/suites/structured.py`, `backend/tests/test_suites.py`.
- TDD RED command: `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`
- TDD RED output: `5 failed, 23 passed, 81 deselected in 2.26s`
- Focused GREEN command: `uv run pytest backend/tests/test_suites.py -k 'structured and (schema or json)' -q`
- Focused GREEN output: `28 passed, 81 deselected in 2.24s`
- Full backend command: `uv run pytest backend/tests -q`
- Full backend output: `214 passed in 5.63s`
- Scoped diff command: `git diff --check -- backend/evalbench/suites/structured.py backend/tests/test_suites.py`
- Scoped diff output: passed with no output.
- No real target or judge calls were made, and no secrets were used.
