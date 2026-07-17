"""Minimal HTTP API over the EvalBench registry, runner, and store."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from evalbench.config import get_settings
from evalbench.models import (
    BatchRunEntry,
    BatchRunRequest,
    BatchRunResponse,
    MetricRecord,
    ResultsResponse,
    RunConfig,
    SuiteResult,
)
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
    """Initialize the default store at startup and release it at shutdown.

    In cloud mode (s3_db_bucket set), default_engine/default_session_factory
    are never used - get_session_factory() takes the S3-backed cloud branch
    for every request instead. Skipping init here matters because Lambda's
    filesystem is read-only outside /tmp, and default_engine points at a
    relative ./evalbench.db path that can't be created there.
    """
    if not get_settings().s3_db_bucket:
        await init_db(default_engine)
    try:
        yield
    finally:
        if not get_settings().s3_db_bucket:
            await default_engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in get_settings().cors_allowed_origins.split(",")
        if origin.strip()
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTTP Bearer security for optional auth
security = HTTPBearer(auto_error=False)


async def get_session_factory() -> SessionFactory:
    """Return the session factory, re-downloading from S3 in cloud mode."""
    settings = get_settings()

    # In cloud mode, re-download the database on every request for freshness
    if settings.s3_db_bucket:
        from evalbench.cloud import db_sync

        db_path = Path(tempfile.gettempdir()) / "evalbench_cloud.db"
        db_sync.download_db(settings.s3_db_bucket, settings.s3_db_key, db_path)
        # Create an engine pointing to the just-downloaded database
        cloud_engine = create_engine(
            database_url=f"sqlite+aiosqlite:///{db_path}"
        )
        # download_db no-ops when the S3 object doesn't exist yet (fresh
        # deployment, or a run still in flight before its first upload), so
        # the schema may be missing on the just-downloaded file.
        await init_db(cloud_engine)
        return create_session_factory(cloud_engine)

    return default_session_factory


def get_run_executor() -> RunExecutor:
    """Return the default synchronous run dependency."""
    return execute_run


def _resolve_suite(name: str) -> Suite:
    try:
        return get_suite(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def verify_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]
) -> None:
    """Verify Bearer token if auth is required."""
    settings = get_settings()
    if not settings.require_auth:
        return

    if credentials is None:
        raise HTTPException(status_code=401, detail="Authorization required")

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required")

    if not settings.admin_token or credentials.credentials != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid token")


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
    _: Annotated[None, Depends(verify_token)] = None,
) -> dict[str, str]:
    """Execute one run to persistence before returning its identifier."""
    if get_settings().s3_db_bucket:
        raise HTTPException(
            status_code=400, detail="Use /runs/async in cloud deployments"
        )
    _resolve_suite(config.suite)
    result = await run_executor(config, session_factory=session_factory)
    return {"run_id": result.run_id}


def _start_run(
    suite: Suite,
    domain: str,
    models: list[str],
    judge_model: str,
    run_status_table: str,
    runner_function: str,
) -> str:
    """Create the run_status entry and invoke the runner Lambda; return its run_id."""
    import uuid
    from evalbench.cloud import lambda_invoke, run_status

    run_id = str(uuid.uuid4())
    tasks = suite.load_tasks(domain)
    deduped_models = list(dict.fromkeys(models))
    run_status.create_status(
        run_status_table, run_id, total=len(tasks) * len(deduped_models)
    )
    lambda_invoke.invoke_runner_async(
        runner_function,
        run_id,
        RunConfig(suite=suite.name, domain=domain, models=models, judge_model=judge_model),
    )
    return run_id


@app.post("/runs/async")
async def runs_async(
    config: RunConfig,
    _: Annotated[None, Depends(verify_token)] = None,
) -> dict[str, str]:
    """Trigger an async suite run, returning immediately with run_id."""
    settings = get_settings()
    suite = _resolve_suite(config.suite)

    if not settings.dynamodb_run_status_table:
        raise HTTPException(
            status_code=500,
            detail="Run status table not configured"
        )
    if not settings.runner_lambda_function:
        raise HTTPException(
            status_code=500,
            detail="Runner Lambda not configured"
        )

    run_id = _start_run(
        suite=suite,
        domain=config.domain,
        models=config.models,
        judge_model=config.judge_model,
        run_status_table=settings.dynamodb_run_status_table,
        runner_function=settings.runner_lambda_function,
    )
    return {"run_id": run_id}


@app.post("/runs/batch", response_model=BatchRunResponse)
async def runs_batch(
    request: BatchRunRequest,
    _: Annotated[None, Depends(verify_token)] = None,
) -> BatchRunResponse:
    """Fan out one async run per (suite, domain) pair in the batch."""
    settings = get_settings()

    if not settings.dynamodb_run_status_table:
        raise HTTPException(
            status_code=500,
            detail="Run status table not configured"
        )
    if not settings.runner_lambda_function:
        raise HTTPException(
            status_code=500,
            detail="Runner Lambda not configured"
        )

    # Resolve every suite name before starting any run, so an unknown suite
    # anywhere in the batch aborts the whole request with no side effects.
    resolved = [(spec, _resolve_suite(spec.suite)) for spec in request.suites]

    entries = [
        BatchRunEntry(
            run_id=_start_run(
                suite=suite,
                domain=domain,
                models=spec.models,
                judge_model=request.judge_model,
                run_status_table=settings.dynamodb_run_status_table,
                runner_function=settings.runner_lambda_function,
            ),
            suite=suite.name,
            domain=domain,
        )
        for spec, suite in resolved
        for domain in request.domains
    ]
    return BatchRunResponse(runs=entries)


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


@app.get("/runs/{run_id}/status")
async def run_status_endpoint(run_id: str) -> dict[str, Any]:
    """Return the current progress of an async run."""
    settings = get_settings()

    if not settings.dynamodb_run_status_table:
        raise HTTPException(
            status_code=500,
            detail="Run status table not configured"
        )

    from evalbench.cloud import run_status

    status = run_status.get_status(settings.dynamodb_run_status_table, run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Run not found")

    return status


@app.post("/api/auth/request")
async def auth_request(request: dict[str, str]) -> dict[str, bool]:
    """Request a magic link for sign-in (always returns success regardless of email)."""
    settings = get_settings()
    email = request.get("email", "").strip().lower()

    if not settings.dynamodb_magic_tokens_table:
        raise HTTPException(
            status_code=500,
            detail="Magic token table not configured"
        )

    # Send magic link only if email matches owner
    from evalbench.cloud import auth

    auth.request_magic_link(
        email=email,
        owner_email=settings.owner_email,
        table_name=settings.dynamodb_magic_tokens_table,
        base_url=f"{settings.frontend_url}/run",
        sender_email=settings.ses_sender_email,
        ttl_seconds=settings.magic_link_ttl_seconds,
    )

    # Always return success regardless of match, to avoid leaking owner email
    return {"sent": True}


@app.get("/api/auth/verify")
async def auth_verify(token: str) -> dict[str, str]:
    """Verify a magic link token and return the admin_token if valid."""
    settings = get_settings()

    if not settings.dynamodb_magic_tokens_table:
        raise HTTPException(
            status_code=500,
            detail="Magic token table not configured"
        )

    if not settings.admin_token:
        raise HTTPException(
            status_code=500,
            detail="Admin token not configured"
        )

    from evalbench.cloud import auth

    is_valid = auth.verify_magic_link(
        token=token,
        table_name=settings.dynamodb_magic_tokens_table
    )

    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {"admin_token": settings.admin_token}
