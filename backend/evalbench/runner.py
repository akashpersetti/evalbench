"""Provider-call normalization, task execution, and run orchestration."""

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import statistics
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
from evalbench.registry import get_suite
from evalbench.store import (
    SessionFactory,
    create_engine,
    create_session_factory,
    init_db,
    save_records,
)
from evalbench.suites.base import Suite, Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float


def _empty_estimate() -> Estimate:
    return Estimate(mean=None, n=0, ci_low=None, ci_high=None)


def wilson_interval(values: Sequence[float]) -> Estimate:
    """Return the 95% Wilson interval for binary or fractional successes."""
    n = len(values)
    if n == 0:
        return _empty_estimate()

    z = 1.96
    mean = sum(values) / n
    denominator = 1 + z**2 / n
    center = (mean + z**2 / (2 * n)) / denominator
    half = (
        z
        * math.sqrt((mean * (1 - mean) + z**2 / (4 * n)) / n)
        / denominator
    )
    return Estimate(
        mean=mean,
        n=n,
        ci_low=max(0.0, center - half),
        ci_high=min(1.0, center + half),
    )


def normal_mean_interval(values: Sequence[float]) -> Estimate:
    """Return a 95% normal interval around the sample mean."""
    n = len(values)
    if n == 0:
        return _empty_estimate()

    mean = statistics.fmean(values)
    if n == 1:
        return Estimate(mean=mean, n=n, ci_low=mean, ci_high=mean)

    standard_error = statistics.stdev(values) / math.sqrt(n)
    half = 1.96 * standard_error
    return Estimate(mean=mean, n=n, ci_low=mean - half, ci_high=mean + half)


def non_negative_mean_interval(values: Sequence[float]) -> Estimate:
    """Return a 95% normal interval around the mean, clamped at zero.

    Clamping (rather than bootstrapping) keeps this a small, deterministic
    change to an existing formula instead of introducing a new resampling
    dependency for what is fundamentally the same asymptotic-normal
    approximation used elsewhere in this module. It is a known-conservative
    fix: for genuinely skewed low-count data the true interval is narrower
    than a clamped symmetric one, but the clamped bound is never wrong in
    the way an unclamped negative bound is.
    """
    estimate = normal_mean_interval(values)
    if estimate.ci_low is None:
        return estimate
    return Estimate(
        mean=estimate.mean,
        n=estimate.n,
        ci_low=max(0.0, estimate.ci_low),
        ci_high=estimate.ci_high,
    )


def _binomial_quantile(n: int, probability: float, quantile: float) -> int:
    if probability <= 0.0:
        return 0
    if probability >= 1.0:
        return n

    mode = min(n, max(0, math.floor((n + 1) * probability)))
    masses = [0.0] * (n + 1)
    masses[mode] = 1.0

    failure_odds = (1.0 - probability) / probability
    for successes in range(mode, 0, -1):
        masses[successes - 1] = (
            masses[successes]
            * successes
            / (n - successes + 1)
            * failure_odds
        )

    success_odds = probability / (1.0 - probability)
    for successes in range(mode, n):
        masses[successes + 1] = (
            masses[successes]
            * (n - successes)
            / (successes + 1)
            * success_odds
        )

    target = quantile * math.fsum(masses)
    cumulative = 0.0
    for successes, mass in enumerate(masses):
        cumulative += mass
        if cumulative >= target:
            return successes
    return n


def percentile_interval(values: Sequence[float], q: float) -> Estimate:
    """Return a nearest-rank percentile with a binomial order-statistic CI."""
    n = len(values)
    if n == 0:
        return _empty_estimate()

    ordered = sorted(values)
    estimate_index = min(n - 1, max(0, math.ceil(q * n) - 1))
    estimate = ordered[estimate_index]
    if n == 1:
        return Estimate(mean=estimate, n=n, ci_low=estimate, ci_high=estimate)

    lower_index = min(n - 1, max(0, _binomial_quantile(n, q, 0.025)))
    upper_index = min(n - 1, max(0, _binomial_quantile(n, q, 0.975)))
    return Estimate(
        mean=estimate,
        n=n,
        ci_low=ordered[lower_index],
        ci_high=ordered[upper_index],
    )


def _stacked_breakdown(
    records: Sequence[MetricRecord], metric_key: str
) -> StackedBreakdown | None:
    metric_is_present = any(metric_key in record.metrics for record in records)
    categorical_values = [
        record.metrics[metric_key]
        for record in records
        if not record.refused and metric_key in record.metrics
    ]
    if not metric_is_present or not all(
        value in {0.0, 0.5, 1.0} for value in categorical_values
    ):
        return None

    counts = {
        "clear": categorical_values.count(1.0),
        "partial": categorical_values.count(0.5),
        "failed": categorical_values.count(0.0),
        "refused": sum(record.refused for record in records),
    }
    n = sum(counts.values())
    labels = {
        "clear": "Clear",
        "partial": "Partial",
        "failed": "Failed",
        "refused": "Refused",
    }
    segments = [
        Segment(
            key=key,
            label=labels[key],
            count=counts[key],
            percentage=counts[key] / n * 100,
        )
        for key in ("clear", "partial", "failed", "refused")
    ]
    return StackedBreakdown(metric_key=metric_key, n=n, segments=segments)


_VALID_SUPPORTS = {"proportion", "non_negative", "real"}


def _support_by_metric(suite: Suite) -> dict[str, str]:
    """Map each metric key to its declared statistical support.

    Raises if any metric in ``suite.metric_keys`` has no declared support,
    or an unrecognized one, rather than silently defaulting: a silent
    default on missing metadata is how ``retries_to_valid`` ended up
    routed through an interval with no lower clamp.
    """
    declared: dict[str, str] = {}
    for metadata in suite.display_metrics:
        key = metadata.get("key")
        support = metadata.get("support")
        if support not in _VALID_SUPPORTS:
            raise ValueError(
                f"suite {suite.name!r} metric {key!r} has invalid support "
                f"{support!r}; must be one of {sorted(_VALID_SUPPORTS)}"
            )
        declared[key] = support

    missing = [key for key in suite.metric_keys if key not in declared]
    if missing:
        raise ValueError(
            f"suite {suite.name!r} is missing declared support for metrics: "
            f"{sorted(missing)}"
        )
    return declared


def _interval_for_support(support: str, values: Sequence[float]) -> Estimate:
    if support == "proportion":
        return wilson_interval(values)
    if support == "non_negative":
        return non_negative_mean_interval(values)
    return normal_mean_interval(values)


def aggregate_records(
    *,
    suite: Suite,
    records: Sequence[MetricRecord],
    domain: str,
    exclude_refusals: bool,
) -> ResultsResponse:
    """Aggregate filtered records into matrix and stacked dashboard shapes."""
    selected = [
        record
        for record in records
        if record.suite == suite.name
        and (domain == "overall" or record.domain == domain)
        and not (exclude_refusals and record.refused)
    ]
    grouped: dict[tuple[str, str, str], list[MetricRecord]] = {}
    for record in selected:
        key = (record.model, record.provider, record.model_family)
        grouped.setdefault(key, []).append(record)

    support_by_metric = _support_by_metric(suite)
    rows: list[AggregatedModelRow] = []
    for (model, provider, model_family), model_records in grouped.items():
        metrics: dict[str, Estimate] = {}
        stacked: dict[str, StackedBreakdown] = {}
        for metric_key in suite.metric_keys:
            values = [
                record.metrics[metric_key]
                for record in model_records
                if metric_key in record.metrics
            ]
            metrics[metric_key] = _interval_for_support(
                support_by_metric[metric_key], values
            )

            breakdown = _stacked_breakdown(model_records, metric_key)
            if breakdown is not None:
                stacked[metric_key] = breakdown

        derived = {
            "p95_latency_ms": percentile_interval(
                [record.latency_ms for record in model_records], 0.95
            )
        }
        if "quality_score" in suite.metric_keys:
            derived["cost_adjusted_quality"] = normal_mean_interval(
                [
                    record.metrics["quality_score"] / record.cost_usd
                    for record in model_records
                    if record.cost_usd > 0
                    and "quality_score" in record.metrics
                ]
            )

        rows.append(
            AggregatedModelRow(
                model=model,
                provider=provider,
                model_family=model_family,
                n=len(model_records),
                metrics=metrics,
                derived=derived,
                stacked=stacked,
            )
        )

    if any(row.stacked for row in rows):
        def clear_percentage(row: AggregatedModelRow) -> float:
            if not row.stacked:
                return -1.0
            return next(iter(row.stacked.values())).segments[0].percentage

        rows.sort(
            key=lambda row: (
                -clear_percentage(row),
                row.model,
                row.provider,
                row.model_family,
            )
        )
    else:
        rows.sort(
            key=lambda row: (row.model, row.provider, row.model_family)
        )

    return ResultsResponse(
        suite=suite.name,
        domain=domain,
        exclude_refusals=exclude_refusals,
        rows=rows,
    )


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
        response_format: dict[str, str] | None = None,
    ) -> tuple[Any, CallResult]:
        started_at = time.perf_counter()
        extra: dict[str, Any] = {}
        if response_format is not None:
            extra["response_format"] = response_format
        try:
            response = self._completion_fn(
                model=model,
                messages=messages,
                timeout=timeout_seconds,
                **extra,
            )
        except Exception:
            elapsed_ms = (time.perf_counter() - started_at) * 1_000
            self.calls.append(
                CallResult(
                    text="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    latency_ms=elapsed_ms,
                )
            )
            raise
        elapsed_ms = (time.perf_counter() - started_at) * 1_000

        try:
            text, prompt_tokens, completion_tokens = _normalize_completion_fields(
                response
            )
            result = CallResult(
                text=text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=self._pricing_fn(
                    model, prompt_tokens, completion_tokens
                ),
                latency_ms=elapsed_ms,
            )
        except Exception:
            self.calls.append(
                CallResult(
                    text="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    latency_ms=elapsed_ms,
                )
            )
            raise
        self.calls.append(result)
        return response, result

    def _metered_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        timeout: float,
        response_format: dict[str, str] | None = None,
    ) -> Any:
        response, _ = self._complete_with_model(
            model=model,
            messages=messages,
            timeout_seconds=timeout,
            response_format=response_format,
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
        except Exception:
            elapsed_ms = (time.perf_counter() - started_at) * 1_000
            self.calls.append(
                CallResult(
                    text="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    latency_ms=elapsed_ms,
                )
            )
            raise
        elapsed_ms = (time.perf_counter() - started_at) * 1_000

        try:
            vectors, prompt_tokens = normalize_embedding_response(response)
            result = CallResult(
                text="",
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                cost_usd=self._pricing_fn(selected_embedder, prompt_tokens, 0),
                latency_ms=elapsed_ms,
            )
        except Exception:
            self.calls.append(
                CallResult(
                    text="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    latency_ms=elapsed_ms,
                )
            )
            raise
        self.calls.append(result)
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
        try:
            if task.requires_generation:
                raw_output = context.complete(suite.build_prompt(task)).text
            judge = Judge(
                judge_model,
                completion_fn=context._metered_completion,
                timeout_seconds=timeout_seconds,
            )
            metrics = suite.evaluate(task, raw_output, judge)
        except Exception as exc:
            error = type(exc).__name__
            logger.warning(
                "task %s (model=%s, run=%s) failed evaluation: %s",
                task.id, model, run_id, exc,
            )

        try:
            refused = suite.detect_refusal(raw_output)
        except Exception as exc:
            error = type(exc).__name__
            refused = False
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
    run_id: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SuiteResult:
    """Execute and atomically persist every task/model pair for one run."""
    settings = get_settings()
    suite = get_suite(config.suite)
    tasks = suite.load_tasks(config.domain)
    run_id = run_id or str(uuid.uuid4())
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

    models = list(dict.fromkeys(config.models))
    work_items = [
        (index, task, model)
        for index, (task, model) in enumerate(
            (task, model) for task in tasks for model in models
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
        if on_progress is not None:
            on_progress(completed, len(work_items))
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
