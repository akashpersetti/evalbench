"""Provider-call normalization, task execution, and run orchestration."""

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any
import uuid

import litellm
from pydantic import ValidationError

from evalbench.config import (
    calculate_cost_usd,
    family_for_model,
    get_settings,
    provider_for_model,
    split_pipeline_model,
)
from evalbench.judge import Judge
from evalbench.models import MetricRecord, RunConfig, SuiteResult
from evalbench.registry import get_suite
from evalbench.store import (
    SessionFactory,
    create_engine,
    create_session_factory,
    init_db,
    save_records,
)
from evalbench.suites.base import Suite, Task


@dataclass(frozen=True)
class CallResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalize_completion_fields(response: Any) -> tuple[str, int, int]:
    choices = _field(response, "choices", [])
    message = _field(choices[0], "message")
    text = _field(message, "content", "") or ""
    usage = _field(response, "usage")
    prompt_tokens = int(_field(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(_field(usage, "completion_tokens", 0) or 0)
    return str(text), prompt_tokens, completion_tokens


def normalize_completion_response(
    response: Any, model: str, elapsed_ms: float
) -> CallResult:
    """Convert a LiteLLM-style completion response into stable call metadata."""
    text, prompt_tokens, completion_tokens = _normalize_completion_fields(response)
    return CallResult(
        text=str(text),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=calculate_cost_usd(model, prompt_tokens, completion_tokens),
        latency_ms=elapsed_ms,
    )


def normalize_embedding_response(response: Any) -> tuple[list[list[float]], int]:
    """Extract vectors and billed input tokens from a LiteLLM-style response."""
    data = _field(response, "data", [])
    vectors = [list(_field(item, "embedding", [])) for item in data]
    usage = _field(response, "usage")
    prompt_tokens = int(_field(usage, "prompt_tokens", 0) or 0)
    return vectors, prompt_tokens


class ExecutionContext:
    """Injected provider operations and metering state for one task/model pair."""

    def __init__(
        self,
        *,
        run_id: str,
        model: str,
        task_id: str,
        completion_fn: Callable[..., Any],
        embedding_fn: Callable[..., Any],
        timeout_seconds: float,
        pricing_fn: Callable[[str, int, int], float],
    ) -> None:
        self._run_id = run_id
        self._model = model
        self._task_id = task_id
        self._completion_fn = completion_fn
        self._embedding_fn = embedding_fn
        self._timeout_seconds = timeout_seconds
        self._pricing_fn = pricing_fn
        self.calls: list[CallResult] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def model(self) -> str:
        return self._model

    @property
    def task_id(self) -> str:
        return self._task_id

    def _complete_with_model(
        self,
        *,
        model: str,
        messages: list[dict],
        timeout_seconds: float,
    ) -> tuple[Any, CallResult]:
        started_at = time.perf_counter()
        try:
            response = self._completion_fn(
                model=model,
                messages=messages,
                timeout=timeout_seconds,
            )
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1_000

        text, prompt_tokens, completion_tokens = _normalize_completion_fields(response)
        result = CallResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=self._pricing_fn(
                model, prompt_tokens, completion_tokens
            ),
            latency_ms=elapsed_ms,
        )
        self.calls.append(result)
        return response, result

    def _metered_completion(
        self, *, model: str, messages: list[dict], timeout: float
    ) -> Any:
        response, _ = self._complete_with_model(
            model=model,
            messages=messages,
            timeout_seconds=timeout,
        )
        return response

    def complete(self, messages: list[dict]) -> CallResult:
        _, result = self._complete_with_model(
            model=self.model,
            messages=messages,
            timeout_seconds=self._timeout_seconds,
        )
        return result

    def embed(
        self, texts: list[str], *, embedder: str | None = None
    ) -> list[list[float]]:
        selected_embedder = embedder or split_pipeline_model(self.model)[0]
        started_at = time.perf_counter()
        try:
            response = self._embedding_fn(
                model=selected_embedder,
                input=texts,
                timeout=self._timeout_seconds,
            )
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1_000

        vectors, prompt_tokens = normalize_embedding_response(response)
        self.calls.append(
            CallResult(
                text="",
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                cost_usd=self._pricing_fn(selected_embedder, prompt_tokens, 0),
                latency_ms=elapsed_ms,
            )
        )
        return vectors


def _execute_one_sync(
    *,
    suite: Suite,
    task: Task,
    model: str,
    run_id: str,
    judge_model: str,
    completion_fn: Callable[..., Any],
    embedding_fn: Callable[..., Any],
    timeout_seconds: float,
    pricing_fn: Callable[[str, int, int], float] = calculate_cost_usd,
) -> MetricRecord:
    """Execute one task/model pair and convert every failure into a record."""
    context = ExecutionContext(
        run_id=run_id,
        model=model,
        task_id=task.id,
        completion_fn=completion_fn,
        embedding_fn=embedding_fn,
        timeout_seconds=timeout_seconds,
        pricing_fn=pricing_fn,
    )
    raw_output = ""
    metrics: dict[str, float] = {}
    error: str | None = None
    refused = False
    task._execution_context = context

    try:
        if task.requires_generation:
            raw_output = context.complete(suite.build_prompt(task)).text
        judge = Judge(
            judge_model,
            completion_fn=context._metered_completion,
            timeout_seconds=timeout_seconds,
        )
        metrics = suite.evaluate(task, raw_output, judge)
        refused = suite.detect_refusal(raw_output)
    except Exception as exc:
        error = type(exc).__name__
        refused = suite.detect_refusal(raw_output)
    finally:
        task._execution_context = None

    if error is None:
        normalized_metrics = {
            key: float(value) for key, value in metrics.items()
        }
    else:
        normalized_metrics = {
            key: float(metrics.get(key, 0.0)) for key in suite.metric_keys
        }
    return MetricRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        suite=suite.name,
        domain=task.domain,
        model=model,
        provider=provider_for_model(model),
        model_family=family_for_model(model),
        task_id=task.id,
        latency_ms=sum(call.latency_ms for call in context.calls),
        prompt_tokens=sum(call.prompt_tokens for call in context.calls),
        completion_tokens=sum(call.completion_tokens for call in context.calls),
        cost_usd=sum(call.cost_usd for call in context.calls),
        error=error,
        refused=refused,
        metrics=normalized_metrics,
        created_at=datetime.now(timezone.utc),
    )


async def execute_run(
    config: RunConfig,
    *,
    session_factory: SessionFactory | None = None,
    completion_fn: Callable[..., Any] | None = None,
    embedding_fn: Callable[..., Any] | None = None,
) -> SuiteResult:
    """Execute and atomically persist every task/model pair for one run."""
    settings = get_settings()
    suite = get_suite(config.suite)
    tasks = suite.load_tasks(config.domain)
    run_id = str(uuid.uuid4())
    selected_completion = (
        completion_fn if completion_fn is not None else litellm.completion
    )
    selected_embedding = (
        embedding_fn if embedding_fn is not None else litellm.embedding
    )
    engine = None
    if session_factory is None:
        engine = create_engine()
        await init_db(engine)
        session_factory = create_session_factory(engine)

    work_items = [
        (index, task, model)
        for index, (task, model) in enumerate(
            (task, model) for task in tasks for model in config.models
        )
    ]
    semaphore = asyncio.Semaphore(settings.max_concurrency)
    completed = 0

    async def execute_item(
        index: int, source_task: Task, model: str
    ) -> tuple[int, MetricRecord]:
        nonlocal completed
        task = source_task.model_copy(deep=True)
        async with semaphore:
            record = await asyncio.to_thread(
                _execute_one_sync,
                suite=suite,
                task=task,
                model=model,
                run_id=run_id,
                judge_model=config.judge_model,
                completion_fn=selected_completion,
                embedding_fn=selected_embedding,
                timeout_seconds=settings.litellm_timeout_seconds,
            )
        completed += 1
        print(
            f"run_id={run_id} progress={completed}/{len(work_items)} "
            f"error={record.error or 'None'}"
        )
        return index, record

    try:
        indexed_records = await asyncio.gather(
            *(
                execute_item(index, task, model)
                for index, task, model in work_items
            )
        )
        records = [
            record for _, record in sorted(indexed_records, key=lambda item: item[0])
        ]
        await save_records(session_factory, records)
        return SuiteResult(run_id=run_id, records=records)
    finally:
        if engine is not None:
            await engine.dispose()


async def _run_cli(config: RunConfig) -> SuiteResult:
    engine = create_engine()
    try:
        await init_db(engine)
        factory = create_session_factory(engine)
        return await execute_run(config, session_factory=factory)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one registered suite from command-line arguments."""
    try:
        settings = get_settings()
        parser = argparse.ArgumentParser(description="Run an EvalBench suite")
        parser.add_argument("--suite", required=True)
        parser.add_argument(
            "--domain",
            required=True,
            choices=[
                "overall",
                "software",
                "finance",
                "legal",
                "medical",
                "physics",
            ],
        )
        parser.add_argument("--models", required=True)
        parser.add_argument("--judge-model", default=settings.judge_model)
        arguments = parser.parse_args(argv)
        config = RunConfig(
            suite=arguments.suite,
            domain=arguments.domain,
            models=[
                model.strip()
                for model in arguments.models.split(",")
                if model.strip()
            ],
            judge_model=arguments.judge_model,
        )
        result = asyncio.run(_run_cli(config))
    except (SystemExit, ValidationError) as exc:
        print(f"error={type(exc).__name__}")
        return int(exc.code) if isinstance(exc, SystemExit) else 2
    except Exception as exc:
        print(f"error={type(exc).__name__}")
        return 1

    print(f"run_id={result.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
