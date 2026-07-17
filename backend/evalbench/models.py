"""Stable public data contracts shared across EvalBench phases."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MetricRecord(BaseModel):
    id: str
    run_id: str
    suite: str
    domain: str
    model: str
    provider: str
    model_family: str
    task_id: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error: str | None
    refused: bool
    metrics: dict[str, float]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class RunConfig(BaseModel):
    suite: str
    domain: Literal[
        "overall", "software", "finance", "legal", "medical", "physics"
    ]
    models: list[str] = Field(min_length=1)
    judge_model: str = "anthropic/claude-sonnet-4-5"


ConcreteDomain = Literal["software", "finance", "legal", "medical", "physics"]


class BatchSuiteSpec(BaseModel):
    suite: str
    models: list[str] = Field(min_length=1)


class BatchRunRequest(BaseModel):
    domains: list[ConcreteDomain] = Field(min_length=1)
    suites: list[BatchSuiteSpec] = Field(min_length=1)
    judge_model: str = "anthropic/claude-sonnet-4-5"


class BatchRunEntry(BaseModel):
    run_id: str
    suite: str
    domain: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: Literal["pending", "running", "done", "error"]
    completed: int
    total: int
    error: str | None = None


class BatchRunResponse(BaseModel):
    runs: list[BatchRunEntry]


class SuiteResult(BaseModel):
    run_id: str
    records: list[MetricRecord]


class Estimate(BaseModel):
    mean: float | None
    n: int
    ci_low: float | None
    ci_high: float | None


class Segment(BaseModel):
    key: Literal["clear", "partial", "failed", "refused"]
    label: Literal["Clear", "Partial", "Failed", "Refused"]
    count: int
    percentage: float


class StackedBreakdown(BaseModel):
    metric_key: str
    n: int
    segments: list[Segment]


class AggregatedModelRow(BaseModel):
    model: str
    provider: str
    model_family: str
    n: int
    metrics: dict[str, Estimate]
    derived: dict[str, Estimate]
    stacked: dict[str, StackedBreakdown]


class ResultsResponse(BaseModel):
    suite: str
    domain: str
    exclude_refusals: bool
    rows: list[AggregatedModelRow]
