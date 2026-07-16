"""Provider-call normalization and per-task execution metering."""

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

from evalbench.config import calculate_cost_usd, split_pipeline_model


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

    def complete(self, messages: list[dict]) -> CallResult:
        started_at = time.perf_counter()
        try:
            response = self._completion_fn(
                model=self.model,
                messages=messages,
                timeout=self._timeout_seconds,
            )
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1_000

        text, prompt_tokens, completion_tokens = _normalize_completion_fields(response)
        result = CallResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=self._pricing_fn(
                self.model, prompt_tokens, completion_tokens
            ),
            latency_ms=elapsed_ms,
        )
        self.calls.append(result)
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
