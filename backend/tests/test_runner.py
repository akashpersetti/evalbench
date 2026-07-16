import logging
import math
import random
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import litellm
import pytest
from httpx import ASGITransport, AsyncClient

import evalbench.api.app as api_module
import evalbench.judge as judge_module
import evalbench.registry as registry_module
import evalbench.runner as runner_module
from evalbench.config import (
    Settings,
    calculate_cost_usd,
    family_for_model,
    provider_for_model,
    split_pipeline_model,
)
from evalbench.models import MetricRecord, RunConfig, SuiteResult
from evalbench.store import (
    create_engine,
    create_session_factory,
    get_run_records,
    init_db,
    save_records,
)
from evalbench.suites.base import Suite, Task


@pytest.mark.parametrize(
    ("model", "provider", "family"),
    [
        ("openai/gpt-4o", "openai", "OpenAI"),
        ("anthropic/claude-sonnet-4-5", "anthropic", "Anthropic"),
        ("gemini/gemini-2.5-pro", "gemini", "Gemini"),
        ("xai/grok-4", "xai", "XAI"),
        ("openrouter/openai/gpt-4o", "openrouter", "OpenRouter"),
        ("voyage-3", "openrouter", "Voyage"),
        ("voyage/voyage-3", "openrouter", "Voyage"),
        ("cohere", "openrouter", "Cohere"),
        ("cohere/embed-v4.0", "openrouter", "Cohere"),
        (
            "openai/text-embedding-3-small::fixed_512",
            "openai",
            "OpenAI",
        ),
    ],
)
def test_known_model_metadata(model: str, provider: str, family: str) -> None:
    assert provider_for_model(model) == provider
    assert family_for_model(model) == family


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            "openai/text-embedding-3-small::fixed_512",
            ("openai/text-embedding-3-small", "fixed_512"),
        ),
        ("openai/gpt-4o", ("openai/gpt-4o", None)),
    ],
)
def test_split_pipeline_model(
    model: str, expected: tuple[str, str | None]
) -> None:
    assert split_pipeline_model(model) == expected


@pytest.mark.parametrize(
    ("model", "prompt_tokens", "completion_tokens", "expected"),
    [
        ("openai/gpt-5.6", 200_000, 10_000, 1.3),
        ("openai/gpt-4o", 400_000, 100_000, 2.0),
        ("anthropic/claude-sonnet-4-5", 100_000, 20_000, 0.6),
        ("openai/text-embedding-3-small", 1_000_000, 0, 0.02),
        (
            "openai/text-embedding-3-small::recursive",
            500_000,
            0,
            0.01,
        ),
        ("voyage-3", 1_000_000, 0, 0.06),
        ("voyage/voyage-3", 1_000_000, 0, 0.06),
        ("cohere", 1_000_000, 0, 0.12),
        ("cohere/embed-v4.0", 1_000_000, 0, 0.12),
    ],
)
def test_calculate_known_model_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    expected: float,
) -> None:
    assert calculate_cost_usd(model, prompt_tokens, completion_tokens) == pytest.approx(
        expected
    )


def test_unknown_model_cost_is_zero_and_logs_only_model_name(caplog) -> None:
    model = "unknown-provider/unknown-model"

    with caplog.at_level(logging.WARNING, logger="evalbench.config"):
        cost = calculate_cost_usd(model, 123_456, 654_321)

    assert cost == 0.0
    assert len(caplog.records) == 1
    assert model in caplog.text
    assert "123456" not in caplog.text
    assert "654321" not in caplog.text


class FakeCompletion:
    def __init__(
        self,
        contents: str | list[str] = "synthetic response",
        *,
        prompt_tokens: int = 400_000,
        completion_tokens: int = 100_000,
        include_usage: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.contents = [contents] if isinstance(contents, str) else list(contents)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.include_usage = include_usage
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        content = self.contents.pop(0)
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        if self.include_usage:
            response.usage = SimpleNamespace(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
            )
        return response


class FakeEmbedding:
    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        *,
        prompt_tokens: int = 1_000_000,
        include_usage: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.vectors = (
            [[0.1, 0.2], [0.3, 0.4]] if vectors is None else vectors
        )
        self.prompt_tokens = prompt_tokens
        self.include_usage = include_usage
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        response = SimpleNamespace(
            data=[SimpleNamespace(embedding=vector) for vector in self.vectors]
        )
        if self.include_usage:
            response.usage = SimpleNamespace(prompt_tokens=self.prompt_tokens)
        return response


def make_context(
    *,
    model: str = "openai/gpt-4o",
    completion: FakeCompletion | None = None,
    embedding: FakeEmbedding | None = None,
    timeout_seconds: float = 4.25,
    pricing_fn=calculate_cost_usd,
) -> runner_module.ExecutionContext:
    return runner_module.ExecutionContext(
        run_id="run-1",
        model=model,
        task_id="task-1",
        completion_fn=completion or FakeCompletion(),
        embedding_fn=embedding or FakeEmbedding(),
        timeout_seconds=timeout_seconds,
        pricing_fn=pricing_fn,
    )


def test_clean_first_import_and_construction_have_no_external_side_effects(
    tmp_path: Path,
) -> None:
    script = textwrap.dedent(
        """
        import importlib
        import os
        import sys
        from pathlib import Path

        import litellm
        import evalbench.store as store_module

        workdir = Path(sys.argv[1])
        os.chdir(workdir)

        attempted_calls = []
        schema_initializations = []

        def fail_completion(**kwargs):
            attempted_calls.append(("completion", kwargs))
            raise AssertionError("real LiteLLM call attempted")

        def fail_embedding(**kwargs):
            attempted_calls.append(("embedding", kwargs))
            raise AssertionError("real LiteLLM call attempted")

        litellm.completion = fail_completion
        litellm.embedding = fail_embedding

        async def fail_init_db(*args, **kwargs):
            schema_initializations.append((args, kwargs))
            raise AssertionError("schema initialized during import")

        store_module.init_db = fail_init_db

        assert "evalbench.judge" not in sys.modules
        assert "evalbench.runner" not in sys.modules
        assert "evalbench.api.app" not in sys.modules
        judge_module = importlib.import_module("evalbench.judge")
        runner_module = importlib.import_module("evalbench.runner")
        api_module = importlib.import_module("evalbench.api.app")

        judge_module.Judge("openai/gpt-4o", timeout_seconds=1.0)
        runner_module.ExecutionContext(
            run_id="run-1",
            model="openai/gpt-4o",
            task_id="task-1",
            completion_fn=litellm.completion,
            embedding_fn=litellm.embedding,
            timeout_seconds=1.0,
            pricing_fn=lambda model, prompt_tokens, completion_tokens: 0.0,
        )

        assert attempted_calls == []
        assert schema_initializations == []
        assert api_module.init_db is fail_init_db
        assert not list(workdir.glob("*.db*"))
        """
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_judge_default_callable_is_late_bound_after_import(monkeypatch) -> None:
    completion = FakeCompletion("late-bound response")
    messages = [{"role": "user", "content": "late-bound request"}]
    monkeypatch.setattr(litellm, "completion", completion)

    judge = judge_module.Judge("openai/gpt-4o", timeout_seconds=6.5)

    assert judge.complete_text(messages) == "late-bound response"
    assert completion.calls == [
        {
            "model": "openai/gpt-4o",
            "messages": messages,
            "timeout": 6.5,
        }
    ]


def test_normalize_completion_response_extracts_text_usage_cost_and_latency() -> None:
    response = FakeCompletion()(
        model="openai/gpt-4o", messages=[], timeout=4.25
    )

    result = runner_module.normalize_completion_response(
        response, "openai/gpt-4o", 125.0
    )

    assert result.text == "synthetic response"
    assert result.prompt_tokens == 400_000
    assert result.completion_tokens == 100_000
    assert result.cost_usd == pytest.approx(2.0)
    assert result.latency_ms == pytest.approx(125.0)


def test_normalize_completion_response_defaults_missing_usage_to_zero() -> None:
    response = FakeCompletion(include_usage=False)(
        model="openai/gpt-4o", messages=[], timeout=4.25
    )

    result = runner_module.normalize_completion_response(
        response, "openai/gpt-4o", 5.0
    )

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.cost_usd == 0.0


def test_execution_context_complete_passes_timeout_and_meters_call(monkeypatch) -> None:
    completion = FakeCompletion()
    embedding = FakeEmbedding()
    messages = [{"role": "user", "content": "synthetic request"}]
    clock = iter([10.0, 10.125])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))

    def reject_global_pricing(*args: Any) -> float:
        raise AssertionError("global pricing called instead of injected pricing")

    monkeypatch.setattr(runner_module, "calculate_cost_usd", reject_global_pricing)
    pricing_calls: list[tuple[str, int, int]] = []

    def pricing_fn(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing_calls.append((model, prompt_tokens, completion_tokens))
        return 3.5

    context = make_context(
        completion=completion,
        embedding=embedding,
        pricing_fn=pricing_fn,
    )

    result = context.complete(messages)

    assert completion.calls[0]["model"] == "openai/gpt-4o"
    assert completion.calls[0]["messages"] is messages
    assert completion.calls[0]["timeout"] == 4.25
    assert pricing_calls == [("openai/gpt-4o", 400_000, 100_000)]
    assert result.cost_usd == 3.5
    assert result.latency_ms == pytest.approx(125.0)
    assert context.calls == [result]


def test_normalize_embedding_response_extracts_vectors_and_usage() -> None:
    response = FakeEmbedding()(
        model="openai/text-embedding-3-small", input=[], timeout=4.25
    )

    vectors, prompt_tokens = runner_module.normalize_embedding_response(response)

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert prompt_tokens == 1_000_000


def test_normalize_embedding_response_defaults_missing_usage_to_zero() -> None:
    response = FakeEmbedding(include_usage=False)(
        model="openai/text-embedding-3-small", input=[], timeout=4.25
    )

    vectors, prompt_tokens = runner_module.normalize_embedding_response(response)

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert prompt_tokens == 0


@pytest.mark.parametrize(
    ("model", "explicit_embedder", "expected_embedder"),
    [
        (
            "openai/text-embedding-3-small::fixed_512",
            None,
            "openai/text-embedding-3-small",
        ),
        (
            "openai/gpt-4o",
            "openai/text-embedding-3-small",
            "openai/text-embedding-3-small",
        ),
    ],
)
def test_execution_context_embed_selects_model_and_meters_call(
    monkeypatch,
    model: str,
    explicit_embedder: str | None,
    expected_embedder: str,
) -> None:
    completion = FakeCompletion()
    embedding = FakeEmbedding()
    texts = ["synthetic one", "synthetic two"]
    clock = iter([20.0, 20.05])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))
    pricing_calls: list[tuple[str, int, int]] = []

    def pricing_fn(name: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing_calls.append((name, prompt_tokens, completion_tokens))
        return 0.75

    context = make_context(
        model=model,
        completion=completion,
        embedding=embedding,
        pricing_fn=pricing_fn,
    )

    vectors = context.embed(texts, embedder=explicit_embedder)

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert embedding.calls[0]["model"] == expected_embedder
    assert embedding.calls[0]["input"] is texts
    assert embedding.calls[0]["timeout"] == 4.25
    assert pricing_calls == [(expected_embedder, 1_000_000, 0)]
    assert context.calls[0].text == ""
    assert context.calls[0].prompt_tokens == 1_000_000
    assert context.calls[0].completion_tokens == 0
    assert context.calls[0].cost_usd == 0.75
    assert context.calls[0].latency_ms == pytest.approx(50.0)


def test_failed_completion_retains_elapsed_latency_without_usage_or_real_call(
    monkeypatch,
) -> None:
    completion = FakeCompletion(error=TimeoutError("synthetic timeout"))
    attempted_real_calls: list[dict[str, Any]] = []

    def reject_real_provider_call(**kwargs: Any) -> None:
        attempted_real_calls.append(kwargs)
        raise AssertionError("real LiteLLM call attempted")

    monkeypatch.setattr(litellm, "completion", reject_real_provider_call)
    monkeypatch.setattr(litellm, "embedding", reject_real_provider_call)
    clock = iter([30.0, 30.25])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))
    context = make_context(
        completion=completion,
        pricing_fn=lambda *_: (_ for _ in ()).throw(
            AssertionError("failed call must not be priced")
        ),
    )

    with pytest.raises(TimeoutError, match="synthetic timeout"):
        context.complete([])

    assert attempted_real_calls == []
    assert len(completion.calls) == 1
    assert len(context.calls) == 1
    failed_call = context.calls[0]
    assert failed_call.text == ""
    assert failed_call.prompt_tokens == 0
    assert failed_call.completion_tokens == 0
    assert failed_call.cost_usd == 0.0
    assert failed_call.latency_ms == pytest.approx(250.0)


def test_failed_embedding_retains_elapsed_latency_without_usage_or_real_call(
    monkeypatch,
) -> None:
    embedding = FakeEmbedding(error=TimeoutError("synthetic timeout"))
    attempted_real_calls: list[dict[str, Any]] = []

    def reject_real_provider_call(**kwargs: Any) -> None:
        attempted_real_calls.append(kwargs)
        raise AssertionError("real LiteLLM call attempted")

    monkeypatch.setattr(litellm, "completion", reject_real_provider_call)
    monkeypatch.setattr(litellm, "embedding", reject_real_provider_call)
    clock = iter([40.0, 40.125])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))
    context = make_context(
        embedding=embedding,
        pricing_fn=lambda *_: (_ for _ in ()).throw(
            AssertionError("failed call must not be priced")
        ),
    )

    with pytest.raises(TimeoutError, match="synthetic timeout"):
        context.embed([])

    assert attempted_real_calls == []
    assert len(embedding.calls) == 1
    assert len(context.calls) == 1
    failed_call = context.calls[0]
    assert failed_call.text == ""
    assert failed_call.prompt_tokens == 0
    assert failed_call.completion_tokens == 0
    assert failed_call.cost_usd == 0.0
    assert failed_call.latency_ms == pytest.approx(125.0)


def test_completion_normalization_failure_retains_latency_in_error_record(
    monkeypatch,
) -> None:
    class CompletionSuite(Suite):
        name = "completion-normalization"
        metric_keys = ["score"]
        display_metrics = []

        def load_tasks(self, domain: str) -> list[Task]:
            return []

        def build_prompt(self, task: Task) -> list[dict]:
            return [{"role": "user", "content": task.prompt}]

        def evaluate(
            self, task: Task, raw_output: str, judge: judge_module.Judge
        ) -> dict[str, float]:
            return {"score": 1.0}

    completion_calls: list[dict[str, Any]] = []

    def malformed_completion(**kwargs: Any) -> SimpleNamespace:
        completion_calls.append(kwargs)
        return SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=91, completion_tokens=17),
        )

    clock = iter([50.0, 50.2])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))
    task = Task(
        id="completion-normalization-1",
        domain="software",
        prompt="synthetic malformed completion",
    )

    record = runner_module._execute_one_sync(
        suite=CompletionSuite(),
        task=task,
        model="openai/gpt-4o",
        run_id="run-completion-normalization",
        judge_model="anthropic/claude-sonnet-4-5",
        completion_fn=malformed_completion,
        embedding_fn=FakeEmbedding(),
        timeout_seconds=3.0,
        pricing_fn=lambda *_: (_ for _ in ()).throw(
            AssertionError("malformed response must not be priced")
        ),
    )

    assert len(completion_calls) == 1
    assert record.error == "IndexError"
    assert record.latency_ms == pytest.approx(200.0)
    assert record.prompt_tokens == 0
    assert record.completion_tokens == 0
    assert record.cost_usd == 0.0
    assert record.metrics == {"score": 0.0}
    assert task._execution_context is None


def test_embedding_normalization_failure_retains_latency_in_error_record(
    monkeypatch,
) -> None:
    class EmbeddingSuite(Suite):
        name = "embedding-normalization"
        metric_keys = ["score"]
        display_metrics = []

        def load_tasks(self, domain: str) -> list[Task]:
            return []

        def build_prompt(self, task: Task) -> list[dict]:
            return []

        def evaluate(
            self, task: Task, raw_output: str, judge: judge_module.Judge
        ) -> dict[str, float]:
            task._execution_context.embed([task.prompt])
            return {"score": 1.0}

    embedding_calls: list[dict[str, Any]] = []

    def malformed_embedding(**kwargs: Any) -> SimpleNamespace:
        embedding_calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=None)],
            usage=SimpleNamespace(prompt_tokens=73),
        )

    clock = iter([60.0, 60.125])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))
    task = Task(
        id="embedding-normalization-1",
        domain="physics",
        prompt="synthetic malformed embedding",
        requires_generation=False,
    )

    record = runner_module._execute_one_sync(
        suite=EmbeddingSuite(),
        task=task,
        model="openai/text-embedding-3-small::fixed_512",
        run_id="run-embedding-normalization",
        judge_model="anthropic/claude-sonnet-4-5",
        completion_fn=FakeCompletion(),
        embedding_fn=malformed_embedding,
        timeout_seconds=3.0,
        pricing_fn=lambda *_: (_ for _ in ()).throw(
            AssertionError("malformed response must not be priced")
        ),
    )

    assert len(embedding_calls) == 1
    assert record.error == "TypeError"
    assert record.latency_ms == pytest.approx(125.0)
    assert record.prompt_tokens == 0
    assert record.completion_tokens == 0
    assert record.cost_usd == 0.0
    assert record.metrics == {"score": 0.0}
    assert task._execution_context is None


def test_judge_complete_text_uses_injected_callable_timeout_and_rng() -> None:
    completion = FakeCompletion("plain text")
    rng = random.Random(17)
    messages = [{"role": "user", "content": "synthetic request"}]
    judge = judge_module.Judge(
        "openai/gpt-4o",
        completion_fn=completion,
        timeout_seconds=9.5,
        rng=rng,
    )

    result = judge.complete_text(messages)

    assert result == "plain text"
    assert completion.calls[0]["model"] == "openai/gpt-4o"
    assert completion.calls[0]["messages"] is messages
    assert completion.calls[0]["timeout"] == 9.5
    assert judge.rng is rng


def test_judge_uses_configured_timeout_when_not_explicit(monkeypatch) -> None:
    completion = FakeCompletion("plain text")
    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(litellm_timeout_seconds=12.0),
    )
    judge = judge_module.Judge("openai/gpt-4o", completion_fn=completion)

    judge.complete_text([])

    assert completion.calls[0]["timeout"] == 12.0


def test_judge_parses_unfenced_and_single_json_fence() -> None:
    completion = FakeCompletion(
        [
            '  {"kind": "unfenced"}  ',
            '```json\n{"kind": "fenced"}\n```',
        ]
    )
    judge = judge_module.Judge(
        "openai/gpt-4o", completion_fn=completion, timeout_seconds=3.0
    )

    assert judge.complete_json([]) == {"kind": "unfenced"}
    assert judge.complete_json([]) == {"kind": "fenced"}


@pytest.mark.parametrize("content", ["not json", "[]", '{"missing": true'])
def test_judge_malformed_json_raises_named_error(content: str) -> None:
    completion = FakeCompletion(content)
    judge = judge_module.Judge(
        "openai/gpt-4o", completion_fn=completion, timeout_seconds=3.0
    )

    with pytest.raises(judge_module.JudgeResponseError):
        judge.complete_json([])


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [(-0.2, 0.0), (0.4, 0.4), (1.7, 1.0)],
)
def test_score_free_text_clamps_judge_score(raw_score: float, expected: float) -> None:
    completion = FakeCompletion(f'{{"score": {raw_score}}}')
    judge = judge_module.Judge(
        "openai/gpt-4o", completion_fn=completion, timeout_seconds=3.0
    )

    score = judge.score_free_text(
        prompt="synthetic prompt",
        expected="synthetic expected",
        actual="synthetic actual",
        rubric="synthetic rubric",
    )

    assert score == expected


def test_score_free_text_preserves_caller_prompt_and_generic_scope() -> None:
    completion = FakeCompletion('{"score": 0.5}')
    judge = judge_module.Judge(
        "openai/gpt-4o", completion_fn=completion, timeout_seconds=3.0
    )
    caller_content = {
        "prompt": "caller-owned prompt 91d7",
        "expected": "caller-owned expected answer 2a6c",
        "actual": "caller-owned actual answer 78f1",
        "rubric": "caller-owned rubric c404",
    }

    judge.score_free_text(**caller_content)

    messages = completion.calls[0]["messages"]
    combined_content = "\n".join(message["content"] for message in messages)
    system_content = "\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    for value in caller_content.values():
        assert value in combined_content
    assert '"score"' in system_content
    assert "0 to 1" in system_content

    scoped_content = combined_content.casefold()
    for forbidden_policy in (
        "litellm",
        "openai",
        "gpt-4o",
        "anthropic",
        "pairwise",
        "answer a",
        "answer b",
        "a/b",
        "a-b",
        "a or b",
        "latency",
    ):
        assert forbidden_policy not in scoped_content


@pytest.mark.parametrize("content", ['{"other": 0.5}', '{"score": "bad"}'])
def test_score_free_text_rejects_malformed_score(content: str) -> None:
    completion = FakeCompletion(content)
    judge = judge_module.Judge(
        "openai/gpt-4o", completion_fn=completion, timeout_seconds=3.0
    )

    with pytest.raises(judge_module.JudgeResponseError):
        judge.score_free_text(
            prompt="synthetic prompt",
            expected="synthetic expected",
            actual="synthetic actual",
            rubric="synthetic rubric",
        )


@pytest.mark.parametrize(
    ("values", "mean", "ci_low", "ci_high"),
    [
        ([1.0, 1.0, 1.0, 1.0], 1.0, 0.5100999795960008, 1.0),
        ([0.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.48990002040399916),
        ([1.0, 0.0, 1.0, 0.0], 0.5, 0.15003570882017148, 0.8499642911798285),
        ([1.0, 0.5], 0.75, 0.19786250921045673, 0.9733234672343529),
    ],
)
def test_wilson_interval_matches_hand_computed_values(
    values: list[float], mean: float, ci_low: float, ci_high: float
) -> None:
    estimate = runner_module.wilson_interval(values)

    assert estimate.n == len(values)
    assert estimate.mean == pytest.approx(mean)
    assert estimate.ci_low == pytest.approx(ci_low)
    assert estimate.ci_high == pytest.approx(ci_high)


def test_wilson_interval_returns_empty_estimate_without_observations() -> None:
    estimate = runner_module.wilson_interval([])

    assert estimate.mean is None
    assert estimate.n == 0
    assert estimate.ci_low is None
    assert estimate.ci_high is None


@pytest.mark.parametrize(
    ("values", "mean", "ci_low", "ci_high"),
    [
        ([7.5], 7.5, 7.5, 7.5),
        ([0.0, 2.0], 1.0, -0.96, 2.96),
    ],
)
def test_normal_mean_interval_matches_hand_computed_values(
    values: list[float], mean: float, ci_low: float, ci_high: float
) -> None:
    estimate = runner_module.normal_mean_interval(values)

    assert estimate.n == len(values)
    assert estimate.mean == pytest.approx(mean)
    assert estimate.ci_low == pytest.approx(ci_low)
    assert estimate.ci_high == pytest.approx(ci_high)


def test_normal_mean_interval_returns_empty_estimate_without_observations() -> None:
    estimate = runner_module.normal_mean_interval([])

    assert estimate.mean is None
    assert estimate.n == 0
    assert estimate.ci_low is None
    assert estimate.ci_high is None


@pytest.mark.parametrize(
    ("values", "mean", "ci_low", "ci_high"),
    [
        ([7.5], 7.5, 7.5, 7.5),
        ([float(value) for value in range(1, 21)], 19.0, 18.0, 20.0),
    ],
)
def test_percentile_interval_matches_nearest_rank_and_order_statistic_ci(
    values: list[float], mean: float, ci_low: float, ci_high: float
) -> None:
    estimate = runner_module.percentile_interval(values, 0.95)

    assert estimate.n == len(values)
    assert estimate.mean == pytest.approx(mean)
    assert estimate.ci_low == pytest.approx(ci_low)
    assert estimate.ci_high == pytest.approx(ci_high)


def test_percentile_interval_returns_empty_estimate_without_observations() -> None:
    estimate = runner_module.percentile_interval([], 0.95)

    assert estimate.mean is None
    assert estimate.n == 0
    assert estimate.ci_low is None
    assert estimate.ci_high is None


class FakeSuite(Suite):
    name = "fake"
    metric_keys = ["score", "output_length"]
    display_metrics = [
        {
            "key": "score",
            "label": "Score",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "output_length",
            "label": "Output length",
            "format": "number",
            "higher_is_better": True,
        },
    ]

    def __init__(self) -> None:
        self.loaded_tasks: list[Task] = []
        self.evaluations: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()

    def load_tasks(self, domain: str) -> list[Task]:
        self.loaded_tasks = [
            Task(
                id="fake-software-1",
                domain="software",
                prompt="secret prompt software 04d5",
                payload={"score": 0.25},
            ),
            Task(
                id="fake-finance-2",
                domain="finance",
                prompt="secret prompt finance e9a1",
                payload={"score": 0.75},
                requires_generation=False,
            ),
        ]
        return self.loaded_tasks

    def build_prompt(self, task: Task) -> list[dict]:
        return [{"role": "user", "content": f"target:{task.id}:{task.prompt}"}]

    def evaluate(
        self, task: Task, raw_output: str, judge: judge_module.Judge
    ) -> dict[str, float]:
        context = task._execution_context
        assert context is not None
        assert judge.model == "anthropic/claude-sonnet-4-5"
        if task.requires_generation:
            assert context.model in raw_output
        else:
            assert raw_output == ""
        context.complete(
            [{"role": "user", "content": f"extra:{task.id}:{task.prompt}"}]
        )
        with self._lock:
            self.evaluations.append((task.id, context.model, raw_output))
        return {
            "score": float(task.payload["score"]),
            "output_length": float(len(raw_output)),
        }


class AggregationSuite(FakeSuite):
    name = "aggregation"
    metric_keys = [
        "quality_score",
        "judge_variance",
        "category_score",
        "missing_metric",
    ]
    display_metrics = [
        {
            "key": "quality_score",
            "label": "Quality",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "judge_variance",
            "label": "Judge variance",
            "format": "number",
            "higher_is_better": False,
        },
        {
            "key": "category_score",
            "label": "Category",
            "format": "number",
            "higher_is_better": True,
        },
        {
            "key": "missing_metric",
            "label": "Missing",
            "format": "percent",
            "higher_is_better": True,
        },
    ]


class ContinuousAggregationSuite(FakeSuite):
    name = "continuous"
    metric_keys = ["continuous_score"]
    display_metrics = [
        {
            "key": "continuous_score",
            "label": "Continuous",
            "format": "number",
            "higher_is_better": True,
        }
    ]


def make_metric_record(
    *,
    record_id: str,
    suite: str = "aggregation",
    domain: str = "software",
    model: str = "shared-model",
    provider: str = "synthetic",
    model_family: str = "Family A",
    latency_ms: float,
    cost_usd: float,
    refused: bool = False,
    metrics: dict[str, float],
) -> MetricRecord:
    return MetricRecord(
        id=record_id,
        run_id="aggregate-run",
        suite=suite,
        domain=domain,
        model=model,
        provider=provider,
        model_family=model_family,
        task_id=f"task-{record_id}",
        latency_ms=latency_ms,
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=cost_usd,
        error=None,
        refused=refused,
        metrics=metrics,
        created_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )


def aggregation_records() -> list[MetricRecord]:
    return [
        make_metric_record(
            record_id="a-clear",
            latency_ms=10.0,
            cost_usd=2.0,
            metrics={
                "quality_score": 1.0,
                "judge_variance": 2.0,
                "category_score": 1.0,
                "undeclared": 99.0,
            },
        ),
        make_metric_record(
            record_id="a-partial-zero-cost",
            latency_ms=20.0,
            cost_usd=0.0,
            metrics={"quality_score": 0.5, "category_score": 0.5},
        ),
        make_metric_record(
            record_id="a-failed",
            latency_ms=30.0,
            cost_usd=1.0,
            metrics={
                "quality_score": 0.0,
                "judge_variance": 4.0,
                "category_score": 0.0,
            },
        ),
        make_metric_record(
            record_id="a-refused",
            latency_ms=40.0,
            cost_usd=4.0,
            refused=True,
            metrics={"quality_score": 1.0},
        ),
        make_metric_record(
            record_id="b-clear-1",
            model_family="Family B",
            latency_ms=5.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0, "category_score": 2.0},
        ),
        make_metric_record(
            record_id="b-clear-2",
            model_family="Family B",
            latency_ms=15.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0, "category_score": 1.0},
        ),
        make_metric_record(
            record_id="wrong-domain",
            domain="finance",
            model="ignored-domain-model",
            model_family="Ignored",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0},
        ),
        make_metric_record(
            record_id="wrong-suite",
            suite="other",
            model="ignored-suite-model",
            model_family="Ignored",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0},
        ),
    ]


def test_aggregate_records_builds_grouped_matrix_derived_and_stacked_shapes() -> None:
    response = runner_module.aggregate_records(
        suite=AggregationSuite(),
        records=aggregation_records(),
        domain="software",
        exclude_refusals=False,
    )

    assert response.suite == "aggregation"
    assert response.domain == "software"
    assert response.exclude_refusals is False
    assert [(row.model, row.provider, row.model_family) for row in response.rows] == [
        ("shared-model", "synthetic", "Family B"),
        ("shared-model", "synthetic", "Family A"),
    ]

    family_b, family_a = response.rows
    assert family_b.n == 2
    assert family_a.n == 4
    assert list(family_a.metrics) == AggregationSuite.metric_keys
    assert "undeclared" not in family_a.metrics

    quality = family_a.metrics["quality_score"]
    assert quality.mean == pytest.approx(0.625)
    assert quality.n == 4
    assert quality.ci_low == pytest.approx(0.21942204237515112)
    assert quality.ci_high == pytest.approx(0.9081029525238491)

    variance = family_a.metrics["judge_variance"]
    assert variance.mean == pytest.approx(3.0)
    assert variance.n == 2
    assert variance.ci_low == pytest.approx(1.04)
    assert variance.ci_high == pytest.approx(4.96)

    missing = family_a.metrics["missing_metric"]
    assert (missing.mean, missing.n, missing.ci_low, missing.ci_high) == (
        None,
        0,
        None,
        None,
    )

    p95 = family_a.derived["p95_latency_ms"]
    assert p95.mean == pytest.approx(40.0)
    assert p95.n == 4
    assert p95.ci_low == pytest.approx(40.0)
    assert p95.ci_high == pytest.approx(40.0)

    cost_adjusted = family_a.derived["cost_adjusted_quality"]
    assert cost_adjusted.mean == pytest.approx(0.25)
    assert cost_adjusted.n == 3
    assert cost_adjusted.ci_low == pytest.approx(-0.03290163190291667)
    assert cost_adjusted.ci_high == pytest.approx(0.5329016319029167)

    for metric_key in ("quality_score", "category_score"):
        stacked = family_a.stacked[metric_key]
        assert stacked.n == 4
        assert [segment.key for segment in stacked.segments] == [
            "clear",
            "partial",
            "failed",
            "refused",
        ]
        assert [segment.label for segment in stacked.segments] == [
            "Clear",
            "Partial",
            "Failed",
            "Refused",
        ]
        assert [segment.count for segment in stacked.segments] == [1, 1, 1, 1]
        assert [segment.percentage for segment in stacked.segments] == pytest.approx(
            [25.0, 25.0, 25.0, 25.0]
        )
    assert "judge_variance" not in family_a.stacked
    assert "missing_metric" not in family_a.stacked
    assert "category_score" not in family_b.stacked


def test_aggregate_records_returns_p95_interval_for_large_history() -> None:
    records = [
        make_metric_record(
            record_id=f"large-history-{value}",
            suite="continuous",
            latency_ms=float(value),
            cost_usd=1.0,
            metrics={"continuous_score": 2.0},
        )
        for value in range(1_030)
    ]

    response = runner_module.aggregate_records(
        suite=ContinuousAggregationSuite(),
        records=records,
        domain="overall",
        exclude_refusals=False,
    )

    estimate = response.rows[0].derived["p95_latency_ms"]
    assert set(estimate.model_dump()) == {"mean", "n", "ci_low", "ci_high"}
    assert estimate.n == 1_030
    assert estimate.mean == 978.0
    assert estimate.ci_low is not None
    assert estimate.ci_high is not None
    assert math.isfinite(estimate.ci_low)
    assert math.isfinite(estimate.ci_high)
    assert estimate.ci_low <= estimate.mean <= estimate.ci_high


def test_aggregate_records_excludes_refusals_with_metric_specific_sample_sizes() -> None:
    response = runner_module.aggregate_records(
        suite=AggregationSuite(),
        records=aggregation_records(),
        domain="software",
        exclude_refusals=True,
    )

    family_a = next(
        row for row in response.rows if row.model_family == "Family A"
    )
    assert family_a.n == 3
    assert family_a.metrics["quality_score"].n == 3
    assert family_a.metrics["judge_variance"].n == 2
    assert family_a.metrics["missing_metric"].n == 0
    assert family_a.derived["p95_latency_ms"].n == 3
    assert family_a.derived["p95_latency_ms"].mean == pytest.approx(30.0)
    assert family_a.derived["p95_latency_ms"].ci_low == pytest.approx(30.0)
    assert family_a.derived["p95_latency_ms"].ci_high == pytest.approx(30.0)
    assert family_a.derived["cost_adjusted_quality"].n == 2
    assert family_a.derived["cost_adjusted_quality"].mean == pytest.approx(0.25)
    assert family_a.derived["cost_adjusted_quality"].ci_low == pytest.approx(-0.24)
    assert family_a.derived["cost_adjusted_quality"].ci_high == pytest.approx(0.74)

    stacked = family_a.stacked["quality_score"]
    assert stacked.n == 3
    assert [segment.count for segment in stacked.segments] == [1, 1, 1, 0]
    assert [segment.percentage for segment in stacked.segments] == pytest.approx(
        [100.0 / 3.0, 100.0 / 3.0, 100.0 / 3.0, 0.0]
    )


def test_aggregate_records_emits_stacked_segments_for_refusal_only_metrics() -> None:
    records = [
        make_metric_record(
            record_id="refused-one",
            latency_ms=10.0,
            cost_usd=1.0,
            refused=True,
            metrics={"quality_score": 1.0, "category_score": 0.0},
        ),
        make_metric_record(
            record_id="refused-two",
            latency_ms=20.0,
            cost_usd=1.0,
            refused=True,
            metrics={"quality_score": 0.0, "category_score": 0.5},
        ),
    ]

    response = runner_module.aggregate_records(
        suite=AggregationSuite(),
        records=records,
        domain="software",
        exclude_refusals=False,
    )

    row = response.rows[0]
    assert list(row.stacked) == ["quality_score", "category_score"]
    for metric_key in row.stacked:
        stacked = row.stacked[metric_key]
        assert stacked.n == 2
        assert [segment.key for segment in stacked.segments] == [
            "clear",
            "partial",
            "failed",
            "refused",
        ]
        assert [segment.label for segment in stacked.segments] == [
            "Clear",
            "Partial",
            "Failed",
            "Refused",
        ]
        assert [segment.count for segment in stacked.segments] == [0, 0, 0, 2]
        assert [segment.percentage for segment in stacked.segments] == [
            0.0,
            0.0,
            0.0,
            100.0,
        ]


def test_aggregate_records_stacked_ties_ignore_input_order() -> None:
    records = [
        make_metric_record(
            record_id="provider-a-family-a",
            provider="provider-a",
            model_family="Family A",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0},
        ),
        make_metric_record(
            record_id="provider-a-family-z",
            provider="provider-a",
            model_family="Family Z",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0},
        ),
        make_metric_record(
            record_id="provider-z-family-a",
            provider="provider-z",
            model_family="Family A",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"quality_score": 1.0},
        ),
    ]
    expected = [
        ("shared-model", "provider-a", "Family A"),
        ("shared-model", "provider-a", "Family Z"),
        ("shared-model", "provider-z", "Family A"),
    ]

    forward = runner_module.aggregate_records(
        suite=AggregationSuite(),
        records=records,
        domain="software",
        exclude_refusals=False,
    )
    reversed_input = runner_module.aggregate_records(
        suite=AggregationSuite(),
        records=list(reversed(records)),
        domain="software",
        exclude_refusals=False,
    )

    assert [
        (row.model, row.provider, row.model_family) for row in forward.rows
    ] == expected
    assert [
        (row.model, row.provider, row.model_family)
        for row in reversed_input.rows
    ] == expected


def test_aggregate_records_sorts_by_model_when_no_stacked_metric_exists() -> None:
    records = [
        make_metric_record(
            record_id="zeta",
            suite="continuous",
            model="zeta",
            model_family="Family Z",
            latency_ms=2.0,
            cost_usd=1.0,
            metrics={"continuous_score": 2.0},
        ),
        make_metric_record(
            record_id="alpha",
            suite="continuous",
            model="alpha",
            model_family="Family A",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"continuous_score": 3.0},
        ),
    ]

    response = runner_module.aggregate_records(
        suite=ContinuousAggregationSuite(),
        records=records,
        domain="overall",
        exclude_refusals=False,
    )

    assert [row.model for row in response.rows] == ["alpha", "zeta"]
    assert all(row.stacked == {} for row in response.rows)
    assert all("cost_adjusted_quality" not in row.derived for row in response.rows)


def test_aggregate_records_fallback_ties_ignore_input_order() -> None:
    records = [
        make_metric_record(
            record_id="provider-a-family-a",
            suite="continuous",
            provider="provider-a",
            model_family="Family A",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"continuous_score": 2.0},
        ),
        make_metric_record(
            record_id="provider-a-family-z",
            suite="continuous",
            provider="provider-a",
            model_family="Family Z",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"continuous_score": 2.0},
        ),
        make_metric_record(
            record_id="provider-z-family-a",
            suite="continuous",
            provider="provider-z",
            model_family="Family A",
            latency_ms=1.0,
            cost_usd=1.0,
            metrics={"continuous_score": 2.0},
        ),
    ]
    expected = [
        ("shared-model", "provider-a", "Family A"),
        ("shared-model", "provider-a", "Family Z"),
        ("shared-model", "provider-z", "Family A"),
    ]

    forward = runner_module.aggregate_records(
        suite=ContinuousAggregationSuite(),
        records=records,
        domain="overall",
        exclude_refusals=False,
    )
    reversed_input = runner_module.aggregate_records(
        suite=ContinuousAggregationSuite(),
        records=list(reversed(records)),
        domain="overall",
        exclude_refusals=False,
    )

    assert [
        (row.model, row.provider, row.model_family) for row in forward.rows
    ] == expected
    assert [
        (row.model, row.provider, row.model_family)
        for row in reversed_input.rows
    ] == expected
    assert all(row.stacked == {} for row in reversed_input.rows)


class DetectorFailingFakeSuite(FakeSuite):
    def __init__(self) -> None:
        super().__init__()
        self.detector_outputs: list[str] = []

    def evaluate(
        self, task: Task, raw_output: str, judge: judge_module.Judge
    ) -> dict[str, float]:
        metrics = super().evaluate(task, raw_output, judge)
        if task.id == "fake-software-1":
            return {"score": metrics["score"]}
        return metrics

    def detect_refusal(self, raw_output: str) -> bool:
        self.detector_outputs.append(raw_output)
        if raw_output:
            raise RuntimeError("synthetic detector failure secret-981b")
        return False


class BoundedFakeCompletion:
    def __init__(self, cap: int = 2) -> None:
        self.cap = cap
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.maximum_active = 0
        self._lock = threading.Lock()
        self._first_wave_ready = threading.Event()

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        with self._lock:
            self.calls.append(kwargs)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == self.cap:
                self._first_wave_ready.set()

        try:
            assert self._first_wave_ready.wait(timeout=5), (
                "bounded fake calls did not overlap"
            )
            content = kwargs["messages"][0]["content"]
            if (
                kwargs["model"] == "openai/gpt-4o"
                and content.startswith("extra:fake-finance-2")
            ):
                raise TimeoutError("synthetic timeout output 524a")
            if content.startswith("target:fake-software-1"):
                if kwargs["model"].startswith("anthropic/"):
                    output = (
                        "I can't assist with that request. "
                        f"model={kwargs['model']} output=secret-61ab"
                    )
                else:
                    output = (
                        f"answer model={kwargs['model']} output=secret-61ab"
                    )
                prompt_tokens, completion_tokens = 7, 3
            else:
                output = "extra output secret-813f"
                prompt_tokens, completion_tokens = 5, 2
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
                usage=SimpleNamespace(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                ),
            )
        finally:
            with self._lock:
                self.active -= 1


def assert_uuid4(value: str) -> None:
    parsed = UUID(value)
    assert parsed.version == 4
    assert str(parsed) == value


async def test_execute_run_records_persists_and_continues_with_bounded_concurrency(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    suite = FakeSuite()
    completion = BoundedFakeCompletion(cap=2)
    database_path = (tmp_path / "runner.db").resolve()
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    await init_db(engine)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        litellm_timeout_seconds=4.0,
        max_concurrency=2,
    )
    config = RunConfig(
        suite="fake",
        domain="overall",
        models=["openai/gpt-4o", "anthropic/claude-sonnet-4-5"],
    )

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(registry_module, "SUITES", {})
            registry_module.register_suite(suite)
            scoped.setattr(runner_module, "get_settings", lambda: settings)
            result = await runner_module.execute_run(
                config,
                session_factory=factory,
                completion_fn=completion,
                embedding_fn=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("unexpected embedding call")
                ),
            )
        persisted = await get_run_records(factory, result.run_id)
    finally:
        await engine.dispose()

    assert_uuid4(result.run_id)
    assert len(result.records) == 4
    assert [
        (record.task_id, record.model) for record in result.records
    ] == [
        ("fake-software-1", "openai/gpt-4o"),
        ("fake-software-1", "anthropic/claude-sonnet-4-5"),
        ("fake-finance-2", "openai/gpt-4o"),
        ("fake-finance-2", "anthropic/claude-sonnet-4-5"),
    ]
    assert all(record.run_id == result.run_id for record in result.records)
    assert len({record.id for record in result.records}) == 4
    for record in result.records:
        assert_uuid4(record.id)

    software_openai, software_anthropic, finance_openai, finance_anthropic = (
        result.records
    )
    assert (software_openai.provider, software_openai.model_family) == (
        "openai",
        "OpenAI",
    )
    assert (software_anthropic.provider, software_anthropic.model_family) == (
        "anthropic",
        "Anthropic",
    )
    assert [record.domain for record in result.records] == [
        "software",
        "software",
        "finance",
        "finance",
    ]
    assert software_openai.metrics == {
        "score": 0.25,
        "output_length": float(
            len("answer model=openai/gpt-4o output=secret-61ab")
        ),
    }
    assert software_anthropic.metrics == {
        "score": 0.25,
        "output_length": float(
            len(
                "I can't assist with that request. "
                "model=anthropic/claude-sonnet-4-5 output=secret-61ab"
            )
        ),
    }
    assert finance_anthropic.metrics == {"score": 0.75, "output_length": 0.0}
    assert [record.refused for record in result.records] == [False, True, False, False]
    assert software_openai.prompt_tokens == 12
    assert software_openai.completion_tokens == 5
    assert software_openai.cost_usd == pytest.approx(
        calculate_cost_usd("openai/gpt-4o", 12, 5)
    )
    assert software_anthropic.prompt_tokens == 12
    assert software_anthropic.completion_tokens == 5
    assert software_anthropic.cost_usd == pytest.approx(
        calculate_cost_usd("anthropic/claude-sonnet-4-5", 12, 5)
    )
    assert finance_anthropic.prompt_tokens == 5
    assert finance_anthropic.completion_tokens == 2
    assert finance_anthropic.cost_usd == pytest.approx(
        calculate_cost_usd("anthropic/claude-sonnet-4-5", 5, 2)
    )
    assert finance_openai.error == "TimeoutError"
    assert finance_openai.prompt_tokens == 0
    assert finance_openai.completion_tokens == 0
    assert finance_openai.cost_usd == 0.0
    assert finance_openai.metrics == {"score": 0.0, "output_length": 0.0}
    assert all(record.created_at.tzinfo is not None for record in result.records)
    assert all(task._execution_context is None for task in suite.loaded_tasks)
    assert completion.maximum_active == 2
    assert completion.maximum_active <= settings.max_concurrency
    assert not any(
        call["messages"][0]["content"].startswith("target:fake-finance-2")
        for call in completion.calls
    )
    assert {(item.task_id, item.model) for item in persisted} == {
        (item.task_id, item.model) for item in result.records
    }
    assert {item.id: item.model_dump() for item in persisted} == {
        item.id: item.model_dump() for item in result.records
    }

    progress = capsys.readouterr().out
    assert f"run_id={result.run_id}" in progress
    assert "progress=4/4" in progress
    assert "error=TimeoutError" in progress
    for secret in (
        "secret prompt",
        "secret-61ab",
        "secret-813f",
        "synthetic timeout output 524a",
    ):
        assert secret not in progress


async def test_refusal_detector_failure_becomes_one_record_and_run_continues(
    monkeypatch, tmp_path: Path
) -> None:
    suite = DetectorFailingFakeSuite()
    completion = BoundedFakeCompletion(cap=1)
    database_path = (tmp_path / "detector-failure.db").resolve()
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    await init_db(engine)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        litellm_timeout_seconds=4.0,
        max_concurrency=1,
    )
    config = RunConfig(
        suite="fake",
        domain="overall",
        models=["anthropic/claude-sonnet-4-5"],
    )

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(registry_module, "SUITES", {})
            registry_module.register_suite(suite)
            scoped.setattr(runner_module, "get_settings", lambda: settings)
            result = await runner_module.execute_run(
                config,
                session_factory=factory,
                completion_fn=completion,
                embedding_fn=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("unexpected embedding call")
                ),
            )
        persisted = await get_run_records(factory, result.run_id)
    finally:
        await engine.dispose()

    failing, remaining = result.records
    assert failing.error == "RuntimeError"
    assert failing.refused is False
    assert failing.metrics == {"score": 0.25, "output_length": 0.0}
    assert failing.prompt_tokens == 12
    assert failing.completion_tokens == 5
    assert failing.cost_usd == pytest.approx(
        calculate_cost_usd("anthropic/claude-sonnet-4-5", 12, 5)
    )
    assert remaining.error is None
    assert remaining.task_id == "fake-finance-2"
    assert len(suite.detector_outputs) == 2
    assert sum(bool(output) for output in suite.detector_outputs) == 1
    assert {record.id for record in persisted} == {
        record.id for record in result.records
    }
    assert len(persisted) == 2


async def test_execute_run_deduplicates_models_in_first_occurrence_order(
    monkeypatch, tmp_path: Path
) -> None:
    suite = FakeSuite()
    completion_calls: list[tuple[str, str]] = []

    def completion(**kwargs: Any) -> SimpleNamespace:
        content = kwargs["messages"][0]["content"]
        completion_calls.append((kwargs["model"], content))
        output = (
            f"answer model={kwargs['model']}"
            if content.startswith("target:")
            else "synthetic extra output"
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )

    database_path = (tmp_path / "duplicate-models.db").resolve()
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    await init_db(engine)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        litellm_timeout_seconds=4.0,
        max_concurrency=2,
    )
    config = RunConfig(
        suite="fake",
        domain="overall",
        models=[
            "anthropic/claude-sonnet-4-5",
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4-5",
        ],
    )

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(registry_module, "SUITES", {})
            registry_module.register_suite(suite)
            scoped.setattr(runner_module, "get_settings", lambda: settings)
            result = await runner_module.execute_run(
                config,
                session_factory=factory,
                completion_fn=completion,
                embedding_fn=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("unexpected embedding call")
                ),
            )
        persisted = await get_run_records(factory, result.run_id)
    finally:
        await engine.dispose()

    expected_pairs = [
        ("fake-software-1", "anthropic/claude-sonnet-4-5"),
        ("fake-software-1", "openai/gpt-4o"),
        ("fake-finance-2", "anthropic/claude-sonnet-4-5"),
        ("fake-finance-2", "openai/gpt-4o"),
    ]
    returned_pairs = [
        (record.task_id, record.model) for record in result.records
    ]
    assert returned_pairs == expected_pairs
    assert len(returned_pairs) == len(set(returned_pairs))
    persisted_pairs = [
        (record.task_id, record.model) for record in persisted
    ]
    assert set(persisted_pairs) == set(expected_pairs)
    assert len(persisted_pairs) == len(set(persisted_pairs))
    assert len(completion_calls) == 6
    assert config.models == [
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-5",
    ]


def test_successful_execution_preserves_omitted_declared_metrics() -> None:
    class SparseMetricSuite(Suite):
        name = "sparse"
        metric_keys = ["score", "sampled_only"]
        display_metrics = []

        def load_tasks(self, domain: str) -> list[Task]:
            return []

        def build_prompt(self, task: Task) -> list[dict]:
            return []

        def evaluate(
            self, task: Task, raw_output: str, judge: judge_module.Judge
        ) -> dict[str, float]:
            return {"score": 1.0}

    task = Task(
        id="sparse-1",
        domain="physics",
        prompt="unused synthetic prompt",
        requires_generation=False,
    )

    record = runner_module._execute_one_sync(
        suite=SparseMetricSuite(),
        task=task,
        model="openai/gpt-4o",
        run_id="run-sparse",
        judge_model="anthropic/claude-sonnet-4-5",
        completion_fn=FakeCompletion(),
        embedding_fn=FakeEmbedding(),
        timeout_seconds=1.0,
    )

    assert record.error is None
    assert record.metrics == {"score": 1.0}
    assert task._execution_context is None


def test_successful_judge_call_is_included_in_universal_metering(
    monkeypatch,
) -> None:
    class JudgeCallingSuite(Suite):
        name = "judge-calling"
        metric_keys = ["score"]
        display_metrics = []

        def load_tasks(self, domain: str) -> list[Task]:
            return []

        def build_prompt(self, task: Task) -> list[dict]:
            return []

        def evaluate(
            self, task: Task, raw_output: str, judge: judge_module.Judge
        ) -> dict[str, float]:
            return {
                "score": judge.score_free_text(
                    prompt=task.prompt,
                    expected="synthetic expected",
                    actual=raw_output,
                    rubric="synthetic rubric",
                )
            }

    completion = FakeCompletion(
        '{"score": 0.75}',
        prompt_tokens=11,
        completion_tokens=4,
    )
    pricing_calls: list[tuple[str, int, int]] = []

    def pricing_fn(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing_calls.append((model, prompt_tokens, completion_tokens))
        return 1.25

    def reject_real_completion(**kwargs: Any) -> None:
        raise AssertionError(f"unexpected real provider call: {kwargs}")

    clock = iter([40.0, 40.25])
    monkeypatch.setattr(litellm, "completion", reject_real_completion)
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(clock))
    task = Task(
        id="judge-only-1",
        domain="legal",
        prompt="synthetic judge-only prompt",
        requires_generation=False,
    )

    record = runner_module._execute_one_sync(
        suite=JudgeCallingSuite(),
        task=task,
        model="openai/gpt-4o",
        run_id="run-judge-metering",
        judge_model="anthropic/claude-sonnet-4-5",
        completion_fn=completion,
        embedding_fn=FakeEmbedding(),
        timeout_seconds=2.0,
        pricing_fn=pricing_fn,
    )

    assert record.error is None
    assert record.metrics == {"score": 0.75}
    assert len(completion.calls) == 1
    assert completion.calls[0]["model"] == "anthropic/claude-sonnet-4-5"
    assert completion.calls[0]["timeout"] == 2.0
    assert pricing_calls == [("anthropic/claude-sonnet-4-5", 11, 4)]
    assert record.prompt_tokens == 11
    assert record.completion_tokens == 4
    assert record.cost_usd == 1.25
    assert record.latency_ms == pytest.approx(250.0)
    assert task._execution_context is None


def test_main_parses_cli_initializes_database_and_returns_zero(
    monkeypatch, capsys
) -> None:
    captured: dict[str, Any] = {}

    class FakeEngine:
        async def dispose(self) -> None:
            captured["disposed"] = True

    async def fake_init_db(engine: object) -> None:
        captured["initialized"] = engine

    async def fake_execute_run(config: RunConfig, **kwargs: Any) -> SuiteResult:
        captured["config"] = config
        captured["session_factory"] = kwargs["session_factory"]
        return SuiteResult(run_id="run-cli", records=[])

    engine = FakeEngine()
    factory = object()
    monkeypatch.setattr(runner_module, "create_engine", lambda: engine)
    monkeypatch.setattr(runner_module, "create_session_factory", lambda value: factory)
    monkeypatch.setattr(runner_module, "init_db", fake_init_db)
    monkeypatch.setattr(runner_module, "execute_run", fake_execute_run)

    exit_code = runner_module.main(
        [
            "--suite",
            "fake",
            "--domain",
            "software",
            "--models",
            "openai/gpt-4o, anthropic/claude-sonnet-4-5",
            "--judge-model",
            "openai/gpt-4o",
        ]
    )

    assert exit_code == 0
    assert captured["initialized"] is engine
    assert captured["session_factory"] is factory
    assert captured["disposed"] is True
    assert captured["config"] == RunConfig(
        suite="fake",
        domain="software",
        models=["openai/gpt-4o", "anthropic/claude-sonnet-4-5"],
        judge_model="openai/gpt-4o",
    )
    assert "run_id=run-cli" in capsys.readouterr().out


def test_main_returns_nonzero_for_invalid_config(monkeypatch) -> None:
    called = False

    async def fake_execute_run(*args: Any, **kwargs: Any) -> SuiteResult:
        nonlocal called
        called = True
        return SuiteResult(run_id="unexpected", records=[])

    monkeypatch.setattr(runner_module, "execute_run", fake_execute_run)

    exit_code = runner_module.main(
        ["--suite", "fake", "--domain", "overall", "--models", ","]
    )

    assert exit_code != 0
    assert called is False


def test_main_returns_nonzero_when_settings_loading_fails(monkeypatch) -> None:
    def fail_settings() -> Settings:
        raise RuntimeError("synthetic settings failure")

    monkeypatch.setattr(runner_module, "get_settings", fail_settings)

    exit_code = runner_module.main(
        [
            "--suite",
            "fake",
            "--domain",
            "overall",
            "--models",
            "openai/gpt-4o",
        ]
    )

    assert exit_code != 0


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.sqlite3'}")
    await init_db(engine)
    factory = create_session_factory(engine)

    async def reject_real_run(*args: Any, **kwargs: Any) -> SuiteResult:
        raise AssertionError("real run executor called")

    monkeypatch.setattr(registry_module, "SUITES", {})
    api_module.app.dependency_overrides[api_module.get_session_factory] = (
        lambda: factory
    )
    api_module.app.dependency_overrides[api_module.get_run_executor] = (
        lambda: reject_real_run
    )
    transport = ASGITransport(app=api_module.app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client, factory
    finally:
        api_module.app.dependency_overrides.clear()
        await engine.dispose()


async def test_api_lifespan_initializes_and_disposes_default_engine(
    monkeypatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeEngine:
        async def dispose(self) -> None:
            events.append(("dispose", self))

    async def fake_init_db(engine: object) -> None:
        events.append(("init", engine))

    engine = FakeEngine()
    monkeypatch.setattr(api_module, "default_engine", engine)
    monkeypatch.setattr(api_module, "init_db", fake_init_db)

    async with api_module.app.router.lifespan_context(api_module.app):
        assert events == [("init", engine)]

    assert events == [("init", engine), ("dispose", engine)]


async def test_api_suites_is_empty_for_phase_one_registry(api_client) -> None:
    client, _ = api_client

    response = await client.get("/suites")

    assert response.status_code == 200
    assert response.json() == []


async def test_api_suites_returns_sorted_exact_metadata(api_client) -> None:
    client, _ = api_client
    registry_module.SUITES.update(
        {
            "fake": FakeSuite(),
            "aggregation": AggregationSuite(),
        }
    )

    response = await client.get("/suites")

    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "aggregation",
            "metric_keys": AggregationSuite.metric_keys,
            "display_metrics": AggregationSuite.display_metrics,
        },
        {
            "name": "fake",
            "metric_keys": FakeSuite.metric_keys,
            "display_metrics": FakeSuite.display_metrics,
        },
    ]
    assert all(
        set(metadata) == {"name", "metric_keys", "display_metrics"}
        for metadata in response.json()
    )


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            "/runs",
            {
                "suite": "missing",
                "domain": "overall",
                "models": ["synthetic/model"],
            },
        ),
        ("GET", "/results?suite=missing&domain=overall", None),
    ],
)
async def test_api_unknown_suite_returns_404(
    api_client, method: str, path: str, payload: dict[str, Any] | None
) -> None:
    client, _ = api_client

    response = await client.request(method, path, json=payload)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {
            "suite": "fake",
            "domain": "unknown",
            "models": ["synthetic/model"],
        },
        {"suite": "fake", "domain": "overall", "models": []},
        {"suite": "fake", "domain": "overall"},
    ],
)
async def test_api_post_runs_enforces_run_config_validation(
    api_client, payload: dict[str, Any]
) -> None:
    client, _ = api_client
    registry_module.SUITES["fake"] = FakeSuite()

    response = await client.post("/runs", json=payload)

    assert response.status_code == 422


async def test_api_post_runs_waits_for_fake_executor_persistence(
    api_client,
) -> None:
    client, factory = api_client
    registry_module.SUITES["fake"] = FakeSuite()
    record = make_metric_record(
        record_id="api-run-record",
        suite="fake",
        latency_ms=12.0,
        cost_usd=0.5,
        metrics={"score": 1.0, "output_length": 4.0},
    ).model_copy(
        update={
            "run_id": "run-api",
            "created_at": datetime.now(timezone.utc),
        }
    )
    received: list[RunConfig] = []

    async def fake_run_executor(
        config: RunConfig, *, session_factory
    ) -> SuiteResult:
        received.append(config)
        assert session_factory is factory
        await save_records(session_factory, [record])
        return SuiteResult(run_id="run-api", records=[record])

    api_module.app.dependency_overrides[api_module.get_run_executor] = (
        lambda: fake_run_executor
    )

    response = await client.post(
        "/runs",
        json={
            "suite": "fake",
            "domain": "software",
            "models": ["synthetic/model"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-api"}
    assert received == [
        RunConfig(
            suite="fake",
            domain="software",
            models=["synthetic/model"],
        )
    ]
    assert await get_run_records(factory, "run-api") == [record]

    raw_response = await client.get("/runs/run-api")
    assert raw_response.status_code == 200
    assert [item["id"] for item in raw_response.json()] == ["api-run-record"]


async def test_api_results_applies_all_filters_and_returns_locked_shapes(
    api_client,
) -> None:
    client, factory = api_client
    registry_module.SUITES["aggregation"] = AggregationSuite()
    now = datetime.now(timezone.utc)

    def api_record(
        record_id: str,
        *,
        family: str,
        created_at: datetime = now,
        domain: str = "software",
        suite: str = "aggregation",
        refused: bool = False,
    ) -> MetricRecord:
        return make_metric_record(
            record_id=record_id,
            suite=suite,
            domain=domain,
            model=f"model-{family}",
            model_family=family,
            latency_ms=10.0,
            cost_usd=1.0,
            refused=refused,
            metrics={"quality_score": 1.0, "category_score": 1.0},
        ).model_copy(update={"created_at": created_at})

    await save_records(
        factory,
        [
            api_record("recent-a", family="Family A"),
            api_record("recent-b", family="Family B"),
            api_record("unselected-family", family="Family C"),
            api_record(
                "old-a",
                family="Family A",
                created_at=now - timedelta(days=31),
            ),
            api_record("wrong-domain", family="Family A", domain="finance"),
            api_record("refused-a", family="Family A", refused=True),
            api_record("wrong-suite", family="Family A", suite="other"),
        ],
    )

    response = await client.get(
        "/results",
        params=[
            ("suite", "aggregation"),
            ("domain", "software"),
            ("window_days", "30"),
            ("exclude_refusals", "true"),
            ("families", "Family A"),
            ("families", "Family B"),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["suite"] == "aggregation"
    assert body["domain"] == "software"
    assert body["exclude_refusals"] is True
    assert {row["model_family"] for row in body["rows"]} == {
        "Family A",
        "Family B",
    }
    assert all(row["n"] == 1 for row in body["rows"])
    for row in body["rows"]:
        for estimate in (*row["metrics"].values(), *row["derived"].values()):
            assert set(estimate) == {"mean", "n", "ci_low", "ci_high"}
        for stacked in row["stacked"].values():
            assert "n" in stacked


@pytest.mark.parametrize("window_days", [None, 7, 30, 90])
async def test_api_results_accepts_legal_or_omitted_window_values(
    api_client, window_days: int | None
) -> None:
    client, _ = api_client
    registry_module.SUITES["aggregation"] = AggregationSuite()
    params: dict[str, str | int] = {
        "suite": "aggregation",
        "domain": "overall",
    }
    if window_days is not None:
        params["window_days"] = window_days

    response = await client.get("/results", params=params)

    assert response.status_code == 200


@pytest.mark.parametrize("window_days", [0, 8, 365, "forever"])
async def test_api_results_rejects_illegal_window_values(
    api_client, window_days: int | str
) -> None:
    client, _ = api_client
    registry_module.SUITES["aggregation"] = AggregationSuite()

    response = await client.get(
        "/results",
        params={
            "suite": "aggregation",
            "domain": "overall",
            "window_days": window_days,
        },
    )

    assert response.status_code == 422


async def test_api_results_rejects_unsupported_domain(api_client) -> None:
    client, _ = api_client
    registry_module.SUITES["aggregation"] = AggregationSuite()

    response = await client.get(
        "/results",
        params={"suite": "aggregation", "domain": "astronomy"},
    )

    assert response.status_code == 422


async def test_api_raw_run_returns_404_when_no_records_exist(api_client) -> None:
    client, _ = api_client

    response = await client.get("/runs/missing-run")

    assert response.status_code == 404


async def test_api_cors_allows_only_local_frontend_origin(api_client) -> None:
    client, _ = api_client
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    }

    response = await client.options("/suites", headers=headers)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "http://localhost:3000"
    )

    rejected = await client.options(
        "/suites",
        headers={**headers, "Origin": "https://example.com"},
    )
    assert "access-control-allow-origin" not in rejected.headers
