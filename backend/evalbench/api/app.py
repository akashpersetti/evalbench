"""Minimal HTTP API over the EvalBench registry, runner, and store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from evalbench.models import MetricRecord, ResultsResponse, RunConfig, SuiteResult
from evalbench.registry import get_suite, list_suites
from evalbench.runner import aggregate_records, execute_run
from evalbench.store import (
    SessionFactory,
    create_engine,
    create_session_factory,
    get_run_records,
    init_db,
    query_records,
)
from evalbench.suites.base import Suite


RunExecutor = Callable[..., Awaitable[SuiteResult]]


class WindowDays(IntEnum):
    SEVEN = 7
    THIRTY = 30
    NINETY = 90

default_engine = create_engine()
default_session_factory = create_session_factory(default_engine)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize the default store at startup and release it at shutdown."""
    await init_db(default_engine)
    try:
        yield
    finally:
        await default_engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_session_factory() -> SessionFactory:
    """Return the default store dependency."""
    return default_session_factory


def get_run_executor() -> RunExecutor:
    """Return the default synchronous run dependency."""
    return execute_run


def _resolve_suite(name: str) -> Suite:
    try:
        return get_suite(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/suites")
async def suites() -> list[dict]:
    """Return stable metadata for every explicitly registered suite."""
    return [
        {
            "name": suite.name,
            "metric_keys": suite.metric_keys,
            "display_metrics": suite.display_metrics,
        }
        for suite in list_suites()
    ]


@app.post("/runs")
async def runs(
    config: RunConfig,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    run_executor: Annotated[RunExecutor, Depends(get_run_executor)],
) -> dict[str, str]:
    """Execute one run to persistence before returning its identifier."""
    _resolve_suite(config.suite)
    result = await run_executor(config, session_factory=session_factory)
    return {"run_id": result.run_id}


@app.get("/results", response_model=ResultsResponse)
async def results(
    suite: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    domain: Literal[
        "overall", "software", "finance", "legal", "medical", "physics"
    ] = "overall",
    window_days: Annotated[WindowDays | None, Query()] = None,
    exclude_refusals: bool = False,
    families: Annotated[list[str] | None, Query()] = None,
) -> ResultsResponse:
    """Query filtered raw records and return their aggregate dashboard shapes."""
    selected_suite = _resolve_suite(suite)
    records = await query_records(
        session_factory,
        suite=suite,
        domain=domain,
        window_days=window_days,
        exclude_refusals=exclude_refusals,
        families=families or (),
    )
    return aggregate_records(
        suite=selected_suite,
        records=records,
        domain=domain,
        exclude_refusals=exclude_refusals,
    )


@app.get("/runs/{run_id}", response_model=list[MetricRecord])
async def raw_run(
    run_id: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> list[MetricRecord]:
    """Return the persisted records for one run."""
    records = await get_run_records(session_factory, run_id)
    if not records:
        raise HTTPException(status_code=404, detail="Run not found")
    return records
