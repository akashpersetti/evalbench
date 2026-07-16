from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

from evalbench.models import MetricRecord
from evalbench.store import (
    MetricRecordRow,
    create_engine,
    create_session_factory,
    get_run_records,
    init_db,
    query_records,
    save_record,
    save_records,
)


@pytest.fixture
async def store(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    database_path = (tmp_path / "evalbench.db").resolve()
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    try:
        await init_db(engine)
        yield engine, factory
    finally:
        await engine.dispose()


def make_record(**overrides: object) -> MetricRecord:
    values: dict[str, object] = {
        "id": "record-1",
        "run_id": "run-1",
        "suite": "structured",
        "domain": "software",
        "model": "openai/gpt-4o",
        "provider": "openai",
        "model_family": "OpenAI",
        "task_id": "task-1",
        "latency_ms": 125.5,
        "prompt_tokens": 23,
        "completion_tokens": 11,
        "cost_usd": 0.0001675,
        "error": None,
        "refused": False,
        "metrics": {"schema_valid": 1.0, "retries": 0.0},
        "created_at": datetime(2026, 7, 15, 12, 0, 0, 123456, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return MetricRecord.model_validate(values)


async def test_engine_and_session_factory_use_required_async_settings(
    tmp_path: Path,
) -> None:
    file_engine = create_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'settings.db').resolve()}"
    )
    memory_engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory = create_session_factory(file_engine)

        assert file_engine.echo is False
        assert not isinstance(file_engine.sync_engine.pool, StaticPool)
        assert isinstance(memory_engine.sync_engine.pool, StaticPool)
        async with factory() as session:
            assert isinstance(session, AsyncSession)
            assert session.autoflush is False
            assert session.sync_session.expire_on_commit is False

        async with file_engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            driver_connection = raw_connection.driver_connection
            assert await connection.scalar(text("SELECT 1")) == 1
        await driver_connection.close()
        async with file_engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await file_engine.dispose()
        await memory_engine.dispose()


async def test_init_db_creates_portable_metric_record_schema(store) -> None:
    engine, _ = store

    async with engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
        database_indexes = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_indexes(
                "metric_records"
            )
        )

    table = MetricRecordRow.__table__
    assert table_names == ["metric_records"]
    assert list(table.columns.keys()) == list(MetricRecord.model_fields)
    assert table.primary_key.columns.keys() == ["id"]
    assert isinstance(table.c.id.type, String)
    assert isinstance(table.c.run_id.type, String)
    assert isinstance(table.c.suite.type, String)
    assert isinstance(table.c.domain.type, String)
    assert isinstance(table.c.model.type, String)
    assert isinstance(table.c.provider.type, String)
    assert isinstance(table.c.model_family.type, String)
    assert isinstance(table.c.task_id.type, String)
    assert isinstance(table.c.latency_ms.type, Float)
    assert isinstance(table.c.prompt_tokens.type, Integer)
    assert isinstance(table.c.completion_tokens.type, Integer)
    assert isinstance(table.c.cost_usd.type, Float)
    assert isinstance(table.c.error.type, String)
    assert isinstance(table.c.refused.type, Boolean)
    assert isinstance(table.c.metrics.type, JSON)
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.error.nullable is True
    assert all(
        not column.nullable for column in table.columns if column.name != "error"
    )
    assert {tuple(index.columns.keys()) for index in table.indexes} == {
        ("run_id",),
        ("suite",),
        ("domain",),
        ("model_family",),
        ("created_at",),
    }
    assert all(not index.unique for index in table.indexes)
    assert {
        (tuple(index["column_names"]), index["unique"])
        for index in database_indexes
    } == {
        (("run_id",), 0),
        (("suite",), 0),
        (("domain",), 0),
        (("model_family",), 0),
        (("created_at",), 0),
    }


async def test_record_round_trips_json_and_utc_timestamp_exactly(store) -> None:
    _, factory = store
    record = make_record(
        error="TimeoutError",
        metrics={"schema_valid": 0.75, "judge_score": 0.625},
        created_at=datetime(
            2026,
            7,
            15,
            8,
            30,
            0,
            987654,
            tzinfo=timezone(timedelta(hours=-4)),
        ),
    )

    await save_record(factory, record)
    restored = await get_run_records(factory, record.run_id)

    assert restored == [record]
    assert restored[0].error == "TimeoutError"
    assert restored[0].metrics == record.metrics
    assert restored[0].created_at == record.created_at
    assert restored[0].created_at.tzinfo is timezone.utc


async def test_save_records_rolls_back_the_entire_batch_on_failure(store) -> None:
    _, factory = store
    first = make_record(id="duplicate")
    duplicate = make_record(id="duplicate", task_id="task-2")

    with pytest.raises(IntegrityError):
        await save_records(factory, [first, duplicate])

    assert await get_run_records(factory, first.run_id) == []


async def test_get_run_records_is_isolated_and_deterministically_sorted(store) -> None:
    _, factory = store
    timestamp = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    records = [
        make_record(
            id="later",
            task_id="task-0",
            created_at=timestamp + timedelta(1),
        ),
        make_record(
            id="task-b",
            task_id="task-b",
            model="model-a",
            created_at=timestamp,
        ),
        make_record(
            id="model-b",
            task_id="task-a",
            model="model-b",
            created_at=timestamp,
        ),
        make_record(
            id="model-a",
            task_id="task-a",
            model="model-a",
            created_at=timestamp,
        ),
        make_record(
            id="other-run",
            run_id="run-2",
            created_at=timestamp - timedelta(1),
        ),
    ]
    await save_records(factory, records)

    restored = await get_run_records(factory, "run-1")

    assert [record.id for record in restored] == [
        "model-a",
        "model-b",
        "task-b",
        "later",
    ]


async def test_query_records_filters_suite_and_specific_domain(store) -> None:
    _, factory = store
    await save_records(
        factory,
        [
            make_record(id="match"),
            make_record(id="wrong-domain", domain="finance"),
            make_record(id="wrong-suite", suite="retrieval"),
        ],
    )

    restored = await query_records(
        factory,
        suite="structured",
        domain="software",
        window_days=None,
        exclude_refusals=False,
        families=[],
    )

    assert [record.id for record in restored] == ["match"]


async def test_query_records_treats_overall_as_no_domain_predicate(store) -> None:
    _, factory = store
    await save_records(
        factory,
        [
            make_record(id="software"),
            make_record(id="finance", domain="finance"),
            make_record(id="other-suite", suite="retrieval", domain="legal"),
        ],
    )

    restored = await query_records(
        factory,
        suite="structured",
        domain="overall",
        window_days=None,
        exclude_refusals=False,
        families=[],
    )

    assert {record.id for record in restored} == {"software", "finance"}


async def test_query_records_applies_refusal_and_family_filters(store) -> None:
    _, factory = store
    await save_records(
        factory,
        [
            make_record(id="openai-clear"),
            make_record(id="openai-refused", refused=True),
            make_record(id="anthropic-clear", model_family="Anthropic"),
            make_record(id="google-refused", model_family="Google", refused=True),
        ],
    )

    openai_records = await query_records(
        factory,
        suite="structured",
        domain="overall",
        window_days=None,
        exclude_refusals=False,
        families=["OpenAI"],
    )
    non_refusals = await query_records(
        factory,
        suite="structured",
        domain="overall",
        window_days=None,
        exclude_refusals=True,
        families=[],
    )

    assert {record.id for record in openai_records} == {
        "openai-clear",
        "openai-refused",
    }
    assert {record.id for record in non_refusals} == {
        "openai-clear",
        "anthropic-clear",
    }


async def test_query_records_includes_exact_cutoff_and_supports_all_time(
    store,
) -> None:
    _, factory = store
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=7)
    await save_records(
        factory,
        [
            make_record(id="before", created_at=cutoff - timedelta(microseconds=1)),
            make_record(id="at-cutoff", created_at=cutoff),
            make_record(id="after", created_at=cutoff + timedelta(microseconds=1)),
        ],
    )

    windowed = await query_records(
        factory,
        suite="structured",
        domain="overall",
        window_days=7,
        exclude_refusals=False,
        families=[],
        now=now,
    )
    all_time = await query_records(
        factory,
        suite="structured",
        domain="overall",
        window_days=None,
        exclude_refusals=False,
        families=[],
        now=now,
    )

    assert {record.id for record in windowed} == {"at-cutoff", "after"}
    assert {record.id for record in all_time} == {"before", "at-cutoff", "after"}


async def test_saved_record_persists_after_writer_session_closes(store) -> None:
    _, factory = store
    record = make_record()

    await save_record(factory, record)
    async with factory() as fresh_session:
        assert not fresh_session.in_transaction()

    assert await get_run_records(factory, record.run_id) == [record]
