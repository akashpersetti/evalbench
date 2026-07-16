import importlib
import logging
import random
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

import evalbench.judge as judge_module
import evalbench.runner as runner_module
from evalbench.config import (
    calculate_cost_usd,
    family_for_model,
    provider_for_model,
    split_pipeline_model,
)


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


def test_import_and_construction_do_not_call_litellm(monkeypatch) -> None:
    attempted_calls: list[str] = []

    def fail_completion(**kwargs: Any) -> None:
        attempted_calls.append("completion")
        raise AssertionError("real LiteLLM call attempted")

    def fail_embedding(**kwargs: Any) -> None:
        attempted_calls.append("embedding")
        raise AssertionError("real LiteLLM call attempted")

    monkeypatch.setattr(litellm, "completion", fail_completion)
    monkeypatch.setattr(litellm, "embedding", fail_embedding)

    importlib.reload(runner_module)
    importlib.reload(judge_module)
    judge_module.Judge("openai/gpt-4o", timeout_seconds=1.0)
    runner_module.ExecutionContext(
        run_id="run-1",
        model="openai/gpt-4o",
        task_id="task-1",
        completion_fn=litellm.completion,
        embedding_fn=litellm.embedding,
        timeout_seconds=1.0,
        pricing_fn=calculate_cost_usd,
    )

    assert attempted_calls == []


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


@pytest.mark.parametrize("operation", ["complete", "embed"])
def test_execution_context_propagates_provider_errors_after_stopping_timer(
    monkeypatch, operation: str
) -> None:
    completion = FakeCompletion(error=RuntimeError("synthetic failure"))
    embedding = FakeEmbedding(error=RuntimeError("synthetic failure"))
    context = make_context(completion=completion, embedding=embedding)
    clock_reads: list[float] = []

    def perf_counter() -> float:
        value = 30.0 + len(clock_reads)
        clock_reads.append(value)
        return value

    monkeypatch.setattr(runner_module.time, "perf_counter", perf_counter)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        if operation == "complete":
            context.complete([])
        else:
            context.embed([])

    assert clock_reads == [30.0, 31.0]
    assert context.calls == []


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
