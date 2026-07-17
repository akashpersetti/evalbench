"""Unit tests for the runner Lambda's orchestration of a suite run."""

from pathlib import Path
from typing import Any

import pytest

import evalbench.runner_lambda as runner_lambda_module
from evalbench.cloud import run_status as run_status_module
from evalbench.config import Settings
from evalbench.models import MetricRecord, RunConfig, SuiteResult


def _make_record(run_id: str) -> MetricRecord:
    from datetime import datetime, timezone

    return MetricRecord(
        id="rec-1",
        run_id=run_id,
        suite="fake",
        domain="software",
        model="openai/gpt-4o",
        provider="openai",
        model_family="OpenAI",
        task_id="task-1",
        latency_ms=10.0,
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.0,
        error=None,
        refused=False,
        metrics={},
        created_at=datetime.now(timezone.utc),
    )


def test_handler_runs_downloads_uploads_and_marks_done(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        s3_db_bucket="evalbench-dev-db",
        s3_db_key="evalbench.db",
        dynamodb_run_status_table="evalbench-dev-run-status",
    )
    monkeypatch.setattr(runner_lambda_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_lambda_module, "_LOCAL_DB_PATH", tmp_path / "run.db")

    calls: list[str] = []
    monkeypatch.setattr(
        runner_lambda_module.db_sync,
        "download_db",
        lambda bucket, key, path: calls.append(f"download:{bucket}/{key}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.db_sync,
        "upload_db",
        lambda bucket, key, path: calls.append(f"upload:{bucket}/{key}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "set_running",
        lambda table, run_id: calls.append(f"running:{run_id}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "increment_completed",
        lambda table, run_id: calls.append(f"progress:{run_id}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "set_done",
        lambda table, run_id: calls.append(f"done:{run_id}"),
    )

    async def fake_execute_run(config: RunConfig, **kwargs: Any) -> SuiteResult:
        kwargs["on_progress"](1, 1)
        return SuiteResult(run_id=kwargs["run_id"], records=[_make_record(kwargs["run_id"])])

    monkeypatch.setattr(runner_lambda_module, "execute_run", fake_execute_run)

    result = runner_lambda_module.handler(
        {
            "run_id": "run-abc",
            "config": {
                "suite": "fake",
                "domain": "software",
                "models": ["openai/gpt-4o"],
            },
        },
        None,
    )

    assert result == {"run_id": "run-abc"}
    assert calls == [
        "download:evalbench-dev-db/evalbench.db",
        "running:run-abc",
        "progress:run-abc",
        "upload:evalbench-dev-db/evalbench.db",
        "done:run-abc",
    ]


def test_handler_marks_error_and_reraises_on_failure(monkeypatch, tmp_path) -> None:
    settings = Settings(
        s3_db_bucket="evalbench-dev-db",
        s3_db_key="evalbench.db",
        dynamodb_run_status_table="evalbench-dev-run-status",
    )
    monkeypatch.setattr(runner_lambda_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_lambda_module, "_LOCAL_DB_PATH", tmp_path / "run.db")
    monkeypatch.setattr(runner_lambda_module.db_sync, "download_db", lambda *a: None)
    monkeypatch.setattr(runner_lambda_module.db_sync, "upload_db", lambda *a: None)
    monkeypatch.setattr(runner_lambda_module.run_status, "set_running", lambda *a: None)

    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "set_error",
        lambda table, run_id, message: errors.append((run_id, message)),
    )

    async def failing_execute_run(config: RunConfig, **kwargs: Any) -> SuiteResult:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(runner_lambda_module, "execute_run", failing_execute_run)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        runner_lambda_module.handler(
            {
                "run_id": "run-fail",
                "config": {
                    "suite": "fake",
                    "domain": "software",
                    "models": ["openai/gpt-4o"],
                },
            },
            None,
        )

    assert errors == [("run-fail", "synthetic failure")]
