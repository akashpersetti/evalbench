"""Stable suite interface and its internal task container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, PrivateAttr

if TYPE_CHECKING:
    from evalbench.judge import Judge


_REFUSAL_PHRASES = ("i can't assist", "i cannot comply", "as an ai")


class Task(BaseModel):
    id: str
    domain: str
    prompt: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requires_generation: bool = True
    _execution_context: Any = PrivateAttr(default=None)


class Suite(ABC):
    name: str
    metric_keys: list[str]
    display_metrics: list[dict]

    @abstractmethod
    def load_tasks(self, domain: str) -> list[Task]: ...

    @abstractmethod
    def build_prompt(self, task: Task) -> list[dict]: ...

    @abstractmethod
    def evaluate(
        self, task: Task, raw_output: str, judge: Judge
    ) -> dict[str, float]: ...

    def detect_refusal(self, raw_output: str) -> bool:
        normalized_output = " ".join(raw_output.lower().split())
        return any(phrase in normalized_output for phrase in _REFUSAL_PHRASES)
