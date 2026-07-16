import json
from datetime import datetime, timezone
from inspect import signature
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
    extract_json,
    model_from_schema,
    validate_output,
)


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
