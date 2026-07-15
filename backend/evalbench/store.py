"""Async persistence for raw EvalBench metric records."""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from evalbench.config import get_settings
from evalbench.models import MetricRecord


class Base(DeclarativeBase):
    """Declarative base for EvalBench persistence models."""


class MetricRecordRow(Base):
    """Portable database representation of :class:`MetricRecord`."""

    __tablename__ = "metric_records"
    __table_args__ = (
        Index("ix_metric_records_run_id", "run_id"),
        Index("ix_metric_records_suite", "suite"),
        Index("ix_metric_records_domain", "domain"),
        Index("ix_metric_records_model_family", "model_family"),
        Index("ix_metric_records_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    suite: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_family: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    refused: Mapped[bool] = mapped_column(Boolean, nullable=False)
    metrics: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """Create an async engine without connecting or creating tables."""

    url = database_url or get_settings().database_url
    parsed_url = make_url(url)
    engine_options: dict[str, object] = {
        "echo": False,
        "pool_pre_ping": True,
    }
    if (
        parsed_url.get_backend_name() == "sqlite"
        and parsed_url.database in (None, "", ":memory:")
    ):
        engine_options["poolclass"] = StaticPool

    return create_async_engine(url, **engine_options)


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Create sessions configured for explicit async transaction boundaries."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def init_db(engine: AsyncEngine) -> None:
    """Create the store schema if it does not already exist."""

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _to_row(record: MetricRecord) -> MetricRecordRow:
    return MetricRecordRow(
        id=record.id,
        run_id=record.run_id,
        suite=record.suite,
        domain=record.domain,
        model=record.model,
        provider=record.provider,
        model_family=record.model_family,
        task_id=record.task_id,
        latency_ms=record.latency_ms,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        cost_usd=record.cost_usd,
        error=record.error,
        refused=record.refused,
        metrics=record.metrics,
        created_at=record.created_at,
    )


def _to_model(row: MetricRecordRow) -> MetricRecord:
    created_at = row.created_at
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)

    return MetricRecord(
        id=row.id,
        run_id=row.run_id,
        suite=row.suite,
        domain=row.domain,
        model=row.model,
        provider=row.provider,
        model_family=row.model_family,
        task_id=row.task_id,
        latency_ms=row.latency_ms,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        cost_usd=row.cost_usd,
        error=row.error,
        refused=row.refused,
        metrics=row.metrics,
        created_at=created_at,
    )


async def save_record(factory: SessionFactory, record: MetricRecord) -> None:
    """Persist one record atomically."""

    async with factory() as session:
        async with session.begin():
            session.add(_to_row(record))


async def save_records(
    factory: SessionFactory, records: Sequence[MetricRecord]
) -> None:
    """Persist all records in one transaction."""

    async with factory() as session:
        async with session.begin():
            session.add_all([_to_row(record) for record in records])


async def get_run_records(
    factory: SessionFactory, run_id: str
) -> list[MetricRecord]:
    """Return one run's records in deterministic order."""

    statement = (
        select(MetricRecordRow)
        .where(MetricRecordRow.run_id == run_id)
        .order_by(
            MetricRecordRow.created_at,
            MetricRecordRow.task_id,
            MetricRecordRow.model,
        )
    )
    async with factory() as session:
        rows = (await session.scalars(statement)).all()
    return [_to_model(row) for row in rows]


async def query_records(
    factory: SessionFactory,
    *,
    suite: str,
    domain: str,
    window_days: int | None,
    exclude_refusals: bool,
    families: Sequence[str],
    now: datetime | None = None,
) -> list[MetricRecord]:
    """Return raw records matching the requested result filters."""

    predicates = [MetricRecordRow.suite == suite]
    if domain != "overall":
        predicates.append(MetricRecordRow.domain == domain)
    if window_days is not None:
        reference_time = now or datetime.now(timezone.utc)
        cutoff = reference_time - timedelta(days=window_days)
        predicates.append(MetricRecordRow.created_at >= cutoff)
    if exclude_refusals:
        predicates.append(MetricRecordRow.refused.is_(False))
    if families:
        predicates.append(MetricRecordRow.model_family.in_(families))

    statement = (
        select(MetricRecordRow)
        .where(*predicates)
        .order_by(
            MetricRecordRow.created_at,
            MetricRecordRow.task_id,
            MetricRecordRow.model,
        )
    )
    async with factory() as session:
        rows = (await session.scalars(statement)).all()
    return [_to_model(row) for row in rows]
