import json
import re
from datetime import datetime, timezone
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticUndefined

import evalbench.judge as judge_module
import evalbench.registry as registry_module
import evalbench.runner as runner_module
from evalbench.models import (
    AggregatedModelRow,
    Estimate,
    MetricRecord,
    ResultsResponse,
    RunConfig,
    Segment,
    StackedBreakdown,
    SuiteResult,
)
from evalbench.suites.base import Suite, Task
from evalbench.suites.structured import (
    StructuredSuite,
    build_retry_messages,
    extract_json,
    field_accuracy,
    iter_expected_leaves,
    model_from_schema,
    validate_output,
)
from evalbench.runner import CallResult


METRIC_RECORD_FIELDS = (
    "id",
    "run_id",
    "suite",
    "domain",
    "model",
    "provider",
    "model_family",
    "task_id",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "error",
    "refused",
    "metrics",
    "created_at",
)

LOCKED_PUBLIC_MODEL_SCHEMAS = {
    MetricRecord: {
        "id": (str, True, PydanticUndefined, None),
        "run_id": (str, True, PydanticUndefined, None),
        "suite": (str, True, PydanticUndefined, None),
        "domain": (str, True, PydanticUndefined, None),
        "model": (str, True, PydanticUndefined, None),
        "provider": (str, True, PydanticUndefined, None),
        "model_family": (str, True, PydanticUndefined, None),
        "task_id": (str, True, PydanticUndefined, None),
        "latency_ms": (float, True, PydanticUndefined, None),
        "prompt_tokens": (int, True, PydanticUndefined, None),
        "completion_tokens": (int, True, PydanticUndefined, None),
        "cost_usd": (float, True, PydanticUndefined, None),
        "error": (str | None, True, PydanticUndefined, None),
        "refused": (bool, True, PydanticUndefined, None),
        "metrics": (dict[str, float], True, PydanticUndefined, None),
        "created_at": (datetime, True, PydanticUndefined, None),
    },
    RunConfig: {
        "suite": (str, True, PydanticUndefined, None),
        "domain": (
            Literal[
                "overall", "software", "finance", "legal", "medical", "physics"
            ],
            True,
            PydanticUndefined,
            None,
        ),
        "models": (list[str], True, PydanticUndefined, None),
        "judge_model": (str, False, "anthropic/claude-sonnet-4-5", None),
    },
    SuiteResult: {
        "run_id": (str, True, PydanticUndefined, None),
        "records": (list[MetricRecord], True, PydanticUndefined, None),
    },
    Estimate: {
        "mean": (float | None, True, PydanticUndefined, None),
        "n": (int, True, PydanticUndefined, None),
        "ci_low": (float | None, True, PydanticUndefined, None),
        "ci_high": (float | None, True, PydanticUndefined, None),
    },
    Segment: {
        "key": (
            Literal["clear", "partial", "failed", "refused"],
            True,
            PydanticUndefined,
            None,
        ),
        "label": (
            Literal["Clear", "Partial", "Failed", "Refused"],
            True,
            PydanticUndefined,
            None,
        ),
        "count": (int, True, PydanticUndefined, None),
        "percentage": (float, True, PydanticUndefined, None),
    },
    StackedBreakdown: {
        "metric_key": (str, True, PydanticUndefined, None),
        "n": (int, True, PydanticUndefined, None),
        "segments": (list[Segment], True, PydanticUndefined, None),
    },
    AggregatedModelRow: {
        "model": (str, True, PydanticUndefined, None),
        "provider": (str, True, PydanticUndefined, None),
        "model_family": (str, True, PydanticUndefined, None),
        "n": (int, True, PydanticUndefined, None),
        "metrics": (dict[str, Estimate], True, PydanticUndefined, None),
        "derived": (dict[str, Estimate], True, PydanticUndefined, None),
        "stacked": (
            dict[str, StackedBreakdown],
            True,
            PydanticUndefined,
            None,
        ),
    },
    ResultsResponse: {
        "suite": (str, True, PydanticUndefined, None),
        "domain": (str, True, PydanticUndefined, None),
        "exclude_refusals": (bool, True, PydanticUndefined, None),
        "rows": (list[AggregatedModelRow], True, PydanticUndefined, None),
    },
}


def metric_record_data() -> dict:
    return {
        "id": "record-1",
        "run_id": "run-1",
        "suite": "structured",
        "domain": "software",
        "model": "openai/gpt-4o",
        "provider": "openai",
        "model_family": "OpenAI",
        "task_id": "structured-software-1",
        "latency_ms": 125.5,
        "prompt_tokens": 23,
        "completion_tokens": 11,
        "cost_usd": 0.0001675,
        "error": None,
        "refused": False,
        "metrics": {"schema_valid": 1.0, "retries": 0.0},
        "created_at": datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    }


@pytest.mark.parametrize(
    ("model", "expected_schema"),
    LOCKED_PUBLIC_MODEL_SCHEMAS.items(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_locked_public_model_schema_is_exact(model: type, expected_schema: dict) -> None:
    actual_schema = {
        name: (
            field.annotation,
            field.is_required(),
            field.default,
            field.default_factory,
        )
        for name, field in model.model_fields.items()
    }

    assert actual_schema == expected_schema


def test_locked_task_model_schema_is_exact() -> None:
    actual_schema = {
        name: (
            field.annotation,
            field.is_required(),
            field.default,
            field.default_factory,
        )
        for name, field in Task.model_fields.items()
    }

    assert actual_schema == {
        "id": (str, True, PydanticUndefined, None),
        "domain": (str, True, PydanticUndefined, None),
        "prompt": (str, True, PydanticUndefined, None),
        "payload": (dict[str, Any], False, PydanticUndefined, dict),
        "requires_generation": (bool, False, True, None),
    }


def test_metric_record_round_trips_all_universal_fields() -> None:
    record = MetricRecord.model_validate(metric_record_data())

    restored = MetricRecord.model_validate(record.model_dump())

    assert restored == record
    assert set(restored.model_fields_set) == set(METRIC_RECORD_FIELDS)
    assert restored.created_at.tzinfo is not None
    assert restored.created_at.utcoffset() is not None


@pytest.mark.parametrize("missing_field", METRIC_RECORD_FIELDS)
def test_metric_record_requires_every_universal_field(missing_field: str) -> None:
    data = metric_record_data()
    del data[missing_field]

    with pytest.raises(ValidationError):
        MetricRecord.model_validate(data)


def test_metric_record_rejects_naive_created_at() -> None:
    data = metric_record_data()
    data["created_at"] = datetime(2026, 7, 15, 12, 0)

    with pytest.raises(ValidationError):
        MetricRecord.model_validate(data)


@pytest.mark.parametrize(
    "domain", ["overall", "software", "finance", "legal", "medical", "physics"]
)
def test_run_config_accepts_each_locked_domain(domain: str) -> None:
    config = RunConfig(suite="structured", domain=domain, models=["openai/gpt-4o"])

    assert config.domain == domain


def test_run_config_rejects_empty_models() -> None:
    with pytest.raises(ValidationError):
        RunConfig(suite="structured", domain="overall", models=[])


def test_run_config_rejects_unknown_domain() -> None:
    with pytest.raises(ValidationError):
        RunConfig(
            suite="structured",
            domain="astronomy",
            models=["openai/gpt-4o"],
        )


def test_run_config_uses_locked_default_judge_model() -> None:
    config = RunConfig(
        suite="structured", domain="overall", models=["openai/gpt-4o"]
    )

    assert config.judge_model == "anthropic/claude-sonnet-4-5"


def test_aggregate_contracts_round_trip_nested_values() -> None:
    estimate = Estimate(mean=0.75, n=4, ci_low=0.5, ci_high=1.0)
    segment = Segment(key="clear", label="Clear", count=3, percentage=75.0)
    breakdown = StackedBreakdown(metric_key="schema_valid", n=4, segments=[segment])
    row = AggregatedModelRow(
        model="openai/gpt-4o",
        provider="openai",
        model_family="OpenAI",
        n=4,
        metrics={"schema_valid": estimate},
        derived={"p95_latency_ms": estimate},
        stacked={"schema_valid": breakdown},
    )
    response = ResultsResponse(
        suite="structured",
        domain="overall",
        exclude_refusals=True,
        rows=[row],
    )

    assert ResultsResponse.model_validate(response.model_dump()) == response
    assert response.rows[0].metrics["schema_valid"] == estimate
    assert response.rows[0].stacked["schema_valid"].segments == [segment]


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("unknown", "Clear"),
        ("clear", "Unknown"),
    ],
)
def test_segment_rejects_values_outside_locked_literals(key: str, label: str) -> None:
    with pytest.raises(ValidationError):
        Segment(key=key, label=label, count=1, percentage=100.0)


def test_suite_result_round_trips_metric_records() -> None:
    result = SuiteResult(run_id="run-1", records=[MetricRecord(**metric_record_data())])

    assert SuiteResult.model_validate(result.model_dump()) == result


def test_task_execution_context_is_private_and_not_serialized() -> None:
    task = Task(
        id="task-1",
        domain="software",
        prompt="Return a JSON object.",
        payload={"schema": {"type": "object"}},
        requires_generation=False,
    )
    context = object()

    task._execution_context = context
    dumped = task.model_dump()
    restored = Task.model_validate(dumped)

    assert task._execution_context is context
    assert "_execution_context" not in dumped
    assert restored._execution_context is None


def test_task_uses_independent_payload_defaults() -> None:
    first = Task(id="task-1", domain="software", prompt="First")
    second = Task(id="task-2", domain="finance", prompt="Second")

    first.payload["changed"] = True

    assert second.payload == {}
    assert first.requires_generation is True


STRUCTURED_SCHEMA = {
    "title": "StructuredOutput",
    "type": "object",
    "properties": {
        "release": {
            "type": "object",
            "properties": {
                "version": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["version", "tags"],
            "additionalProperties": False,
        },
        "status": {"type": "string", "enum": ["draft", "published"]},
        "owner": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["release", "status"],
    "additionalProperties": False,
}

STRUCTURED_DATASET_DOMAINS = (
    "software",
    "finance",
    "legal",
    "medical",
    "physics",
)
STRUCTURED_DATASET_DIR = (
    Path(__file__).resolve().parents[1] / "data" / "structured"
)


def _structured_dataset_rows() -> list[dict[str, Any]]:
    rows = []
    for domain in STRUCTURED_DATASET_DOMAINS:
        path = STRUCTURED_DATASET_DIR / f"{domain}.jsonl"
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def _structured_numeric_leaves(
    schema: dict[str, Any], expected: Any, pointer: str = ""
) -> list[tuple[str, Any, Any]]:
    schema_type = schema.get("type")
    if schema_type == "object":
        return [
            leaf
            for field_name, field_schema in schema["properties"].items()
            for leaf in _structured_numeric_leaves(
                field_schema,
                expected[field_name],
                f"{pointer}/{field_name}",
            )
        ]
    if schema_type == "array":
        return [
            leaf
            for index, item in enumerate(expected)
            for leaf in _structured_numeric_leaves(
                schema["items"], item, f"{pointer}/{index}"
            )
        ]
    if schema_type in {"number", "integer"}:
        return [(schema_type, pointer, expected)]
    return []


def _decimal_numeric_tokens(prompt: str) -> list[float]:
    tokens = re.findall(
        r"(?<![\w.])[-+]?(?:(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+)(?![\w.])",
        prompt,
    )
    return [float(token) for token in tokens]


def test_structured_dataset_has_exact_balanced_files_ids_and_adversarial_split() -> None:
    expected_files = {
        f"{domain}.jsonl" for domain in STRUCTURED_DATASET_DOMAINS
    }

    assert STRUCTURED_DATASET_DIR.is_dir(), (
        f"missing structured dataset directory: {STRUCTURED_DATASET_DIR}"
    )
    assert {
        path.name for path in STRUCTURED_DATASET_DIR.iterdir() if path.is_file()
    } == expected_files

    rows = []
    for domain in STRUCTURED_DATASET_DOMAINS:
        path = STRUCTURED_DATASET_DIR / f"{domain}.jsonl"
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 8
        domain_rows = [json.loads(line) for line in lines]
        assert [row["id"] for row in domain_rows] == [
            f"{domain}-{index:02d}" for index in range(1, 9)
        ]
        assert all(row["domain"] == domain for row in domain_rows)
        assert {
            row["id"] for row in domain_rows if row["adversarial"]
        } == {f"{domain}-{index:02d}" for index in (2, 4, 6, 8)}
        rows.extend(domain_rows)

    ids = [row["id"] for row in rows]
    assert len(rows) == 40
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"(software|finance|legal|medical|physics)-\d{2}", task_id) for task_id in ids)


def test_structured_dataset_rows_have_convertible_schemas_and_valid_expected_values() -> None:
    for row in _structured_dataset_rows():
        assert set(row) == {
            "id",
            "domain",
            "prompt",
            "schema",
            "expected",
            "free_text_fields",
            "adversarial",
        }
        assert isinstance(row["prompt"], str) and row["prompt"].strip()
        assert isinstance(row["adversarial"], bool)

        model = model_from_schema(
            f"Dataset_{row['id'].replace('-', '_')}", row["schema"]
        )
        validated = model.model_validate(row["expected"])

        assert validated.model_dump() == row["expected"]


def test_structured_dataset_number_fields_preserve_float_types_and_decimal_cues() -> None:
    for row in _structured_dataset_rows():
        decimal_tokens = _decimal_numeric_tokens(row["prompt"])
        for schema_type, pointer, value in _structured_numeric_leaves(
            row["schema"], row["expected"]
        ):
            if schema_type == "number":
                assert type(value) is float, pointer
                assert any(token == value for token in decimal_tokens), (
                    row["id"],
                    pointer,
                    value,
                )
            else:
                assert type(value) is int, (row["id"], pointer, value)


def test_structured_legal_06_prompt_requests_defaulted_schedule_fields() -> None:
    row = next(row for row in _structured_dataset_rows() if row["id"] == "legal-06")

    assert "time is unspecified" in row["prompt"]
    assert "days_after_service is 10" in row["prompt"]
    assert "time is 09:00" in row["prompt"]
    assert "days_after_service is 0" in row["prompt"]


def test_structured_dataset_free_text_pointers_resolve_without_expected_leakage() -> None:
    for row in _structured_dataset_rows():
        pointers = row["free_text_fields"]
        expected_leaf_pointers = {
            pointer for pointer, _ in iter_expected_leaves(row["expected"])
        }
        serialized_expected = {
            json.dumps(row["expected"]),
            json.dumps(row["expected"], sort_keys=True),
            json.dumps(row["expected"], sort_keys=True, separators=(",", ":")),
        }

        assert isinstance(pointers, list)
        assert len(pointers) <= 2
        assert len(pointers) == len(set(pointers))
        assert set(pointers).issubset(expected_leaf_pointers)
        assert all(value not in row["prompt"] for value in serialized_expected)
        assert not re.search(
            r"(?i)(?:sk-[a-z0-9]|api[_ -]?key|bearer\s+[a-z0-9]|password\s*[:=])",
            row["prompt"],
        )


def test_structured_dataset_loaders_are_deterministic_and_sorted() -> None:
    suite = StructuredSuite()

    first_overall = suite.load_tasks("overall")
    second_overall = suite.load_tasks("overall")

    assert len(first_overall) == 40
    assert [task.model_dump() for task in first_overall] == [
        task.model_dump() for task in second_overall
    ]
    assert [(task.domain, task.id) for task in first_overall] == sorted(
        (task.domain, task.id) for task in first_overall
    )
    assert all(task.requires_generation is True for task in first_overall)
    assert all(
        set(task.payload)
        == {"schema", "expected", "free_text_fields", "adversarial"}
        for task in first_overall
    )

    for domain in STRUCTURED_DATASET_DOMAINS:
        tasks = suite.load_tasks(domain)
        assert len(tasks) == 8
        assert [task.id for task in tasks] == [
            f"{domain}-{index:02d}" for index in range(1, 9)
        ]
        assert all(task.domain == domain for task in tasks)


def test_structured_dataset_loader_rejects_unknown_domain_before_file_access(
    tmp_path: Path,
) -> None:
    suite = StructuredSuite()
    suite.data_root = tmp_path / "does-not-exist"

    with pytest.raises(ValueError, match="unknown structured domain 'astronomy'"):
        suite.load_tasks("astronomy")


def test_structured_dataset_loader_reports_filename_and_line_for_invalid_json(
    tmp_path: Path,
) -> None:
    valid_row = {
        "id": "software-01",
        "domain": "software",
        "prompt": "Return the supplied status as JSON.",
        "schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        "expected": {"status": "ready"},
        "free_text_fields": [],
        "adversarial": False,
    }
    path = tmp_path / "software.jsonl"
    path.write_text(
        f"\n{json.dumps(valid_row)}\nnot-json\n",
        encoding="utf-8",
    )
    suite = StructuredSuite()
    suite.data_root = tmp_path

    with pytest.raises(ValueError, match=r"software\.jsonl:3"):
        suite.load_tasks("software")


def test_structured_dataset_loader_rejects_malformed_exact_task_id(
    tmp_path: Path,
) -> None:
    row = {
        "id": "software-1",
        "domain": "software",
        "prompt": "Return the supplied status as JSON.",
        "schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        "expected": {"status": "ready"},
        "free_text_fields": [],
        "adversarial": False,
    }
    (tmp_path / "software.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="task id must match exact format software-NN"):
        StructuredSuite(data_root=tmp_path).load_tasks("software")


def test_structured_dataset_loader_rejects_integer_expected_for_number_field(
    tmp_path: Path,
) -> None:
    row = {
        "id": "software-01",
        "domain": "software",
        "prompt": "Return value 1.0 as JSON.",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "expected": {"value": 1},
        "free_text_fields": [],
        "adversarial": False,
    }
    (tmp_path / "software.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="number fields must use exact float values"):
        StructuredSuite(data_root=tmp_path).load_tasks("software")


def test_structured_schema_converts_nested_lists_enums_and_nullable_fields() -> None:
    model = model_from_schema("StructuredOutput", STRUCTURED_SCHEMA)

    value = model.model_validate(
        {
            "release": {"version": 2, "tags": ["stable", "api"]},
            "status": "published",
            "owner": None,
        }
    )

    assert value.release.version == 2
    assert value.release.tags == ["stable", "api"]
    assert value.status == "published"
    assert value.owner is None


def test_structured_schema_rejects_unlisted_enum_value() -> None:
    model = model_from_schema("StructuredOutput", STRUCTURED_SCHEMA)

    with pytest.raises(ValidationError):
        model.model_validate(
            {
                "release": {"version": 2, "tags": ["stable"]},
                "status": "archived",
            }
        )


@pytest.mark.parametrize(
    ("value", "expected_message"),
    [
        (
            {
                "release": {"version": "2", "tags": ["stable"]},
                "status": "draft",
            },
            "int_type",
        ),
        (
            {"release": {"version": 2, "tags": ["stable"]}},
            "status",
        ),
        (
            {
                "release": {"version": 2, "tags": ["stable"]},
                "status": "draft",
                "unexpected": True,
            },
            "extra_forbidden",
        ),
    ],
)
def test_structured_schema_rejects_invalid_values(
    value: dict[str, Any], expected_message: str
) -> None:
    parsed, valid, error = validate_output(json.dumps(value), STRUCTURED_SCHEMA)

    assert parsed == value
    assert valid is False
    assert error is not None
    assert expected_message in error


def test_structured_schema_preserves_an_absent_optional_nullable_field() -> None:
    raw_output = '{"release":{"version":2,"tags":["stable"]},"status":"draft"}'

    parsed, valid, error = validate_output(raw_output, STRUCTURED_SCHEMA)

    assert parsed == {
        "release": {"version": 2, "tags": ["stable"]},
        "status": "draft",
    }
    assert valid is True
    assert error is None


def test_structured_schema_preserves_a_valid_explicit_default() -> None:
    schema = {
        "type": "object",
        "properties": {"retries": {"type": "integer", "default": 3}},
        "required": [],
        "additionalProperties": False,
    }

    model = model_from_schema("Defaulted", schema)

    assert model().retries == 3


def test_structured_schema_rejects_an_invalid_explicit_default_at_its_path() -> None:
    schema = {
        "type": "object",
        "properties": {"retries": {"type": "integer", "default": "wrong"}},
        "required": [],
        "additionalProperties": False,
    }

    with pytest.raises(ValueError, match=r"\$\.properties\.retries"):
        model_from_schema("InvalidDefault", schema)


def test_structured_schema_rejects_unsupported_constructs_with_the_schema_path() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1}},
        "required": ["name"],
    }

    with pytest.raises(ValueError, match=r"\$\.properties\.name"):
        model_from_schema("Unsupported", schema)


@pytest.mark.parametrize(
    ("raw_output", "expected"),
    [
        ('{"answer": 42}', {"answer": 42}),
        ('```json\n{"answer": 42}\n```', {"answer": 42}),
        (
            'Result follows: {"message": "a brace: { and a quote: \\""} Thanks.',
            {"message": 'a brace: { and a quote: "'},
        ),
    ],
)
def test_structured_json_extracts_one_value_from_supported_output_forms(
    raw_output: str, expected: Any
) -> None:
    assert extract_json(raw_output) == expected


@pytest.mark.parametrize(
    ("raw_output", "expected_message"),
    [
        ('{"answer":', "invalid JSON"),
        ('{"answer": 1}\n[2]', "ambiguous"),
        ('{"answer": 1}\n42', "ambiguous"),
        ('{"answer": 1}\n42 43', "ambiguous"),
        ('{"answer": true}\ntrue false', "ambiguous"),
        ('{"answer": 1}\nnot JSON { prose', "ambiguous"),
    ],
)
def test_structured_json_rejects_malformed_or_ambiguous_output(
    raw_output: str, expected_message: str
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        extract_json(raw_output)


METRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "blocked"]},
        "count": {"type": "integer"},
        "approved": {"type": "boolean"},
        "details": {
            "type": "object",
            "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
            "required": ["labels"],
            "additionalProperties": False,
        },
    },
    "required": ["status", "count", "approved", "details"],
    "additionalProperties": False,
}
METRIC_EXPECTED = {
    "status": "ready",
    "count": 2,
    "approved": True,
    "details": {"labels": ["one", "two"]},
}


class FakeStructuredContext:
    def __init__(
        self,
        results: list[CallResult],
        initial_calls: list[CallResult] | None = None,
    ) -> None:
        self._results = list(results)
        self.calls = list(initial_calls or [])
        self.messages: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> CallResult:
        self.messages.append(messages)
        result = self._results.pop(0)
        self.calls.append(result)
        return result


class FakeStructuredJudge:
    def __init__(self, score: float = 0.0) -> None:
        self.score = score
        self.calls: list[dict[str, str]] = []

    def score_free_text(
        self, *, prompt: str, expected: str, actual: str, rubric: str
    ) -> float:
        self.calls.append(
            {
                "prompt": prompt,
                "expected": expected,
                "actual": actual,
                "rubric": rubric,
            }
        )
        return self.score


def structured_metric_task(
    *,
    expected: Any = METRIC_EXPECTED,
    schema: dict[str, Any] = METRIC_SCHEMA,
    free_text_fields: list[str] | None = None,
) -> Task:
    return Task(
        id="structured-metric-1",
        domain="software",
        prompt="Return the requested synthetic record.",
        payload={
            "schema": schema,
            "expected": expected,
            "free_text_fields": free_text_fields or [],
        },
        requires_generation=False,
    )


def structured_call(text: str, cost_usd: float = 0.01) -> CallResult:
    return CallResult(
        text=text,
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=cost_usd,
        latency_ms=20.0,
    )


def test_structured_metrics_valid_first_attempt_are_exact_float_keys() -> None:
    suite = StructuredSuite()
    task = structured_metric_task()
    judge = FakeStructuredJudge()

    metrics = suite.evaluate(task, json.dumps(METRIC_EXPECTED), judge)

    assert metrics == {
        "first_attempt_valid": 1.0,
        "schema_valid": 1.0,
        "retries_to_valid": 0.0,
        "retry_cost_usd": 0.0,
        "field_accuracy": 1.0,
    }
    assert set(metrics) == set(suite.metric_keys)
    assert all(isinstance(value, float) for value in metrics.values())
    assert judge.calls == []


def test_structured_retry_records_only_retry_cost_after_invalid_first_attempt() -> None:
    suite = StructuredSuite()
    task = structured_metric_task()
    context = FakeStructuredContext(
        [structured_call(json.dumps(METRIC_EXPECTED), 0.07)],
        initial_calls=[structured_call("not JSON", 0.99)],
    )
    task._execution_context = context

    metrics = suite.evaluate(task, "not JSON", FakeStructuredJudge())

    assert metrics == {
        "first_attempt_valid": 0.0,
        "schema_valid": 1.0,
        "retries_to_valid": 1.0,
        "retry_cost_usd": 0.07,
        "field_accuracy": 1.0,
    }
    assert [call.cost_usd for call in context.calls] == [0.99, 0.07]


def test_structured_retry_submits_latest_invalid_output_with_validation_feedback() -> None:
    suite = StructuredSuite()
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    task = structured_metric_task(
        expected={"answer": "do not disclose"}, schema=schema
    )
    initial_output = '{"answer": 1}'
    second_output = '{"answer": false}'
    context = FakeStructuredContext(
        [
            structured_call(second_output),
            structured_call('{"answer": "accepted"}'),
        ]
    )
    task._execution_context = context

    metrics = suite.evaluate(task, initial_output, FakeStructuredJudge())

    initial_error = validate_output(initial_output, schema)[2]
    second_error = validate_output(second_output, schema)[2]
    canonical_schema = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    assert metrics["schema_valid"] == 1.0
    assert metrics["retries_to_valid"] == 2.0
    assert context.messages[0][-2] == {"role": "assistant", "content": initial_output}
    assert initial_error in context.messages[0][-1]["content"]
    assert canonical_schema in context.messages[0][-1]["content"]
    assert context.messages[1][-2] == {"role": "assistant", "content": second_output}
    assert second_error in context.messages[1][-1]["content"]
    assert canonical_schema in context.messages[1][-1]["content"]
    assert "do not disclose" not in "\n".join(
        message["content"] for messages in context.messages for message in messages
    )


def test_structured_retry_succeeds_on_fourth_total_attempt() -> None:
    suite = StructuredSuite()
    task = structured_metric_task()
    context = FakeStructuredContext(
        [
            structured_call("bad", 0.01),
            structured_call("still bad", 0.02),
            structured_call(json.dumps(METRIC_EXPECTED), 0.03),
        ]
    )
    task._execution_context = context

    metrics = suite.evaluate(task, "invalid", FakeStructuredJudge())

    assert metrics["first_attempt_valid"] == 0.0
    assert metrics["schema_valid"] == 1.0
    assert metrics["retries_to_valid"] == 3.0
    assert metrics["retry_cost_usd"] == pytest.approx(0.06)
    assert len(context.calls) == 3


def test_structured_retry_reports_failed_schema_after_four_invalid_attempts() -> None:
    suite = StructuredSuite()
    task = structured_metric_task()
    context = FakeStructuredContext([structured_call("invalid") for _ in range(3)])
    task._execution_context = context

    metrics = suite.evaluate(task, "invalid", FakeStructuredJudge())

    assert metrics["first_attempt_valid"] == 0.0
    assert metrics["schema_valid"] == 0.0
    assert metrics["retries_to_valid"] == 3.0
    assert metrics["field_accuracy"] == 0.0


def test_structured_accuracy_keeps_parseable_schema_invalid_output_for_partial_credit() -> None:
    suite = StructuredSuite()
    expected = {"present": 1, "missing": True}
    schema = {
        "type": "object",
        "properties": {"present": {"type": "integer"}, "missing": {"type": "boolean"}},
        "required": ["present", "missing"],
        "additionalProperties": False,
    }
    task = structured_metric_task(expected=expected, schema=schema)
    context = FakeStructuredContext([structured_call('{"present": 1}') for _ in range(3)])
    task._execution_context = context

    metrics = suite.evaluate(task, '{"present": 1}', FakeStructuredJudge())

    assert metrics["schema_valid"] == 0.0
    assert metrics["field_accuracy"] == 0.5


def test_structured_runner_classifies_refusal_after_suite_evaluation() -> None:
    suite = StructuredSuite()
    expected = {"answer": "I can't assist with that request."}
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    task = structured_metric_task(expected=expected, schema=schema)
    task.requires_generation = True
    raw_output = json.dumps(expected)

    def completion(**_: Any) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw_output))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    record = runner_module._execute_one_sync(
        suite=suite,
        task=task,
        model="openai/gpt-4o",
        run_id="structured-refusal",
        judge_model="anthropic/claude-sonnet-4-5",
        completion_fn=completion,
        embedding_fn=lambda **_: None,
        timeout_seconds=1.0,
    )

    assert record.error is None
    assert record.metrics["schema_valid"] == 1.0
    assert record.refused is True


def test_structured_field_accuracy_requires_exact_types_values_and_nested_list_leaves() -> None:
    task = structured_metric_task()
    judge = FakeStructuredJudge()

    assert field_accuracy(task, METRIC_EXPECTED, judge) == 1.0
    assert field_accuracy(
        task,
        {
            "status": "blocked",
            "count": True,
            "approved": 1,
            "details": {"labels": ["wrong", "wrong"]},
        },
        judge,
    ) == 0.0
    assert judge.calls == []


def test_structured_field_accuracy_scores_absent_or_wrong_leaves_as_zero() -> None:
    task = structured_metric_task(
        expected={"left": 1, "right": 2},
        schema={
            "type": "object",
            "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
            "required": ["left", "right"],
            "additionalProperties": False,
        },
    )
    judge = FakeStructuredJudge()

    assert field_accuracy(task, {"left": 1}, judge) == 0.5
    assert field_accuracy(task, {"left": "1", "right": 2}, judge) == 0.5


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ({"items": []}, {}),
        ({"items": []}, {"items": ["unexpected"]}),
        ({"details": {}}, {}),
        ({"details": {}}, {"details": {"unexpected": "value"}}),
    ],
)
def test_structured_field_accuracy_scores_absent_or_wrong_empty_containers_as_zero(
    expected: dict[str, Any], actual: dict[str, Any]
) -> None:
    task = structured_metric_task(expected=expected)

    assert field_accuracy(task, actual, FakeStructuredJudge()) == 0.0


def test_structured_field_accuracy_keeps_top_level_empty_expected_object_vacuously_correct() -> None:
    task = structured_metric_task(expected={})

    assert field_accuracy(task, None, FakeStructuredJudge()) == 1.0


def test_structured_field_accuracy_uses_judge_only_for_declared_free_text_leaves() -> None:
    task = structured_metric_task(
        expected={"summary": "Expected summary", "status": "ready"},
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}, "status": {"type": "string"}},
            "required": ["summary", "status"],
            "additionalProperties": False,
        },
        free_text_fields=["/summary"],
    )
    judge = FakeStructuredJudge(0.25)

    score = field_accuracy(
        task,
        {"summary": "Equivalent wording", "status": "ready"},
        judge,
    )

    assert score == 0.625
    assert judge.calls == [
        {
            "prompt": task.prompt,
            "expected": "Expected summary",
            "actual": "Equivalent wording",
            "rubric": "Semantic equivalence and factual completeness; ignore wording differences.",
        }
    ]


def test_structured_leaf_pointers_escape_rfc_6901_tokens() -> None:
    assert iter_expected_leaves({"a/b": {"c~d": [3]}}) == [("/a~1b/c~0d/0", 3)]


def test_structured_prompts_include_canonical_schema_without_expected_values() -> None:
    suite = StructuredSuite()
    task = structured_metric_task(expected={"secret": "do not reveal"})

    messages = suite.build_prompt(task)
    retry_messages = build_retry_messages(messages, "invalid", "invalid JSON", METRIC_SCHEMA)

    assert messages[0]["role"] == "system"
    assert "only JSON" in messages[0]["content"]
    assert json.dumps(METRIC_SCHEMA, sort_keys=True, separators=(",", ":")) in messages[1]["content"]
    assert "do not reveal" not in "\n".join(message["content"] for message in messages)
    assert retry_messages[:2] == messages
    assert retry_messages[2] == {"role": "assistant", "content": "invalid"}
    assert "invalid JSON" in retry_messages[3]["content"]
    assert json.dumps(METRIC_SCHEMA, sort_keys=True, separators=(",", ":")) in retry_messages[3]["content"]
    assert "do not reveal" not in "\n".join(
        message["content"] for message in retry_messages
    )


class MissingEvaluateSuite(Suite):
    name = "incomplete"
    metric_keys = ["score"]
    display_metrics = []

    def load_tasks(self, domain: str) -> list[Task]:
        return []

    def build_prompt(self, task: Task) -> list[dict]:
        return []


class CompleteSuite(MissingEvaluateSuite):
    def evaluate(
        self, task: Task, raw_output: str, judge: object
    ) -> dict[str, float]:
        return {"score": 1.0}


class DependencyUsingSuite(Suite):
    name = "dependency-using"
    metric_keys = ["score"]
    display_metrics = [
        {
            "key": "score",
            "label": "Score",
            "format": "percent",
            "higher_is_better": True,
        }
    ]

    def load_tasks(self, domain: str) -> list[Task]:
        return [
            Task(
                id="dependency-1",
                domain="software",
                prompt="synthetic dependency prompt",
            )
        ]

    def build_prompt(self, task: Task) -> list[dict]:
        return [{"role": "user", "content": task.prompt}]

    def evaluate(
        self, task: Task, raw_output: str, judge: judge_module.Judge
    ) -> dict[str, float]:
        context_output = task._execution_context.complete([]).text
        judge_output = judge.complete_text([])
        return {"score": float(bool(context_output and judge_output))}


def test_suite_cannot_omit_abstract_evaluate() -> None:
    with pytest.raises(TypeError):
        MissingEvaluateSuite()


def test_suite_enforces_all_locked_abstract_methods() -> None:
    assert Suite.__abstractmethods__ == {"load_tasks", "build_prompt", "evaluate"}


def test_suite_method_signatures_match_locked_contract() -> None:
    assert str(signature(Suite.load_tasks)) == "(self, domain: 'str') -> 'list[Task]'"
    assert str(signature(Suite.build_prompt)) == "(self, task: 'Task') -> 'list[dict]'"
    assert str(signature(Suite.evaluate)) == (
        "(self, task: 'Task', raw_output: 'str', judge: 'Judge') "
        "-> 'dict[str, float]'"
    )


@pytest.mark.parametrize(
    "raw_output",
    [
        "I can't assist with that request.",
        "I CANNOT COMPLY with that request.",
        "As an AI, I am unable to help.",
        "I\n  can't\tassist with that request.",
    ],
)
def test_default_refusal_detection_is_case_and_whitespace_insensitive(
    raw_output: str,
) -> None:
    assert CompleteSuite().detect_refusal(raw_output) is True


@pytest.mark.parametrize(
    "raw_output",
    [
        "The answer is 42.",
        "I can assist with that request.",
        "As an airline, we publish route guidance.",
        "This system is described as an artificial intelligence model.",
    ],
)
def test_default_refusal_detection_allows_ordinary_answers(raw_output: str) -> None:
    assert CompleteSuite().detect_refusal(raw_output) is False


def named_suite(name: str) -> CompleteSuite:
    suite = CompleteSuite()
    suite.name = name
    return suite


def test_register_suite_rejects_duplicate_name(monkeypatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(registry_module, "SUITES", {})
        first = named_suite("duplicate")
        registry_module.register_suite(first)

        with pytest.raises(ValueError, match="duplicate"):
            registry_module.register_suite(named_suite("duplicate"))

        assert registry_module.get_suite("duplicate") is first


def test_get_suite_unknown_name_lists_registered_choices(monkeypatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(registry_module, "SUITES", {})
        registry_module.register_suite(named_suite("beta"))
        registry_module.register_suite(named_suite("alpha"))

        with pytest.raises(KeyError) as exc_info:
            registry_module.get_suite("missing")

        message = str(exc_info.value)
        assert "missing" in message
        assert "alpha" in message
        assert "beta" in message


def test_list_suites_sorts_by_name(monkeypatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(registry_module, "SUITES", {})
        registry_module.register_suite(named_suite("zeta"))
        registry_module.register_suite(named_suite("alpha"))

        assert [suite.name for suite in registry_module.list_suites()] == [
            "alpha",
            "zeta",
        ]


@pytest.mark.parametrize(
    "suite",
    registry_module.list_suites(),
    ids=lambda suite: suite.name,
)
def test_registered_suite_contract_is_stable_and_declared(suite: Suite) -> None:
    _assert_suite_contract(suite)


def _contract_completion(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"score": 1.0, "winner": "tie"}'
                )
            )
        ],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )


def _contract_embedding(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        data=[
            SimpleNamespace(embedding=[1.0, 0.0])
            for _ in kwargs["input"]
        ],
        usage=SimpleNamespace(prompt_tokens=len(kwargs["input"])),
    )


def _assert_suite_contract(suite: Suite) -> None:
    assert len(suite.metric_keys) == len(set(suite.metric_keys))
    assert all(
        set(display_metric) == {
            "key",
            "label",
            "format",
            "higher_is_better",
        }
        for display_metric in suite.display_metrics
    )
    assert {
        display_metric["key"] for display_metric in suite.display_metrics
    }.issubset(suite.metric_keys)

    first_tasks = suite.load_tasks("overall")
    second_tasks = suite.load_tasks("overall")
    first_ids = [task.id for task in first_tasks]
    assert first_ids == [task.id for task in second_tasks]
    assert len(first_ids) == len(set(first_ids))

    for task in first_tasks:
        context = runner_module.ExecutionContext(
            run_id="contract-run",
            model="openai/gpt-4o",
            task_id=task.id,
            completion_fn=_contract_completion,
            embedding_fn=_contract_embedding,
            timeout_seconds=1.0,
            pricing_fn=lambda model, prompt_tokens, completion_tokens: 0.0,
        )
        judge = judge_module.Judge(
            "anthropic/claude-sonnet-4-5",
            completion_fn=_contract_completion,
            timeout_seconds=1.0,
        )
        task._execution_context = context
        try:
            metrics = suite.evaluate(task, "", judge)
        finally:
            task._execution_context = None
        assert set(metrics).issubset(suite.metric_keys)
        assert all(isinstance(value, float) for value in metrics.values())


def test_suite_contract_supports_judge_and_execution_context_dependencies() -> None:
    _assert_suite_contract(DependencyUsingSuite())
