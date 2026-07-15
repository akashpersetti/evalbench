from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

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


def test_suite_cannot_omit_abstract_evaluate() -> None:
    with pytest.raises(TypeError):
        MissingEvaluateSuite()


def test_suite_enforces_all_locked_abstract_methods() -> None:
    assert Suite.__abstractmethods__ == {"load_tasks", "build_prompt", "evaluate"}


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
        "This system is described as an artificial intelligence model.",
    ],
)
def test_default_refusal_detection_allows_ordinary_answers(raw_output: str) -> None:
    assert CompleteSuite().detect_refusal(raw_output) is False
