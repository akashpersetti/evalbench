"""Lambda entry point for asynchronous suite runs (invoked only by the api Lambda)."""

import asyncio
from pathlib import Path
from typing import Any

from evalbench.cloud import db_sync
from evalbench.cloud import run_status
from evalbench.config import get_settings
from evalbench.models import RunConfig
from evalbench.runner import execute_run
from evalbench.store import create_engine, create_session_factory, init_db

_LOCAL_DB_PATH = Path("/tmp/evalbench.db")


def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    return asyncio.run(_run(event["run_id"], RunConfig(**event["config"])))


async def _run(run_id: str, config: RunConfig) -> dict[str, str]:
    settings = get_settings()
    run_status.set_running(settings.dynamodb_run_status_table, run_id)

    engine = create_engine(f"sqlite+aiosqlite:///{_LOCAL_DB_PATH}")
    await init_db(engine)
    factory = create_session_factory(engine)

    def on_progress(_completed: int, _total: int) -> None:
        run_status.increment_completed(settings.dynamodb_run_status_table, run_id)

    try:
        await execute_run(
            config,
            session_factory=factory,
            run_id=run_id,
            on_progress=on_progress,
        )
    except Exception as exc:
        run_status.set_error(settings.dynamodb_run_status_table, run_id, str(exc))
        raise
    finally:
        await engine.dispose()

    db_sync.upload_db(
        settings.s3_db_bucket,
        db_sync.run_db_key(settings.s3_db_prefix, run_id),
        _LOCAL_DB_PATH,
    )
    run_status.set_done(settings.dynamodb_run_status_table, run_id)
    return {"run_id": run_id}
