import logging
import random
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import litellm
import pytest

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
from evalbench.models import RunConfig, SuiteResult
from evalbench.store import (
    create_engine,
    create_session_factory,
    get_run_records,
    init_db,
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


def test_clean_first_import_and_construction_do_not_call_litellm() -> None:
    script = textwrap.dedent(
        """
        import importlib
        import sys

        import litellm

        attempted_calls = []

        def fail_completion(**kwargs):
            attempted_calls.append(("completion", kwargs))
            raise AssertionError("real LiteLLM call attempted")

        def fail_embedding(**kwargs):
            attempted_calls.append(("embedding", kwargs))
            raise AssertionError("real LiteLLM call attempted")

        litellm.completion = fail_completion
        litellm.embedding = fail_embedding

        assert "evalbench.judge" not in sys.modules
        assert "evalbench.runner" not in sys.modules
        judge_module = importlib.import_module("evalbench.judge")
        runner_module = importlib.import_module("evalbench.runner")

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
        """
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
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
