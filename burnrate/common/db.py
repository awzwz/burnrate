"""One database per service. Local mode: a SQLite file per service. Cluster: a PostgreSQL database per service.

A service module declares `Base = make_base("cards")` and uses `async with session("cards") as s:`.
No service ever opens another service's database.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import JSON, DateTime, MetaData, String, TypeDecorator, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from burnrate.common.config import settings

_bases: dict[str, type[DeclarativeBase]] = {}
_engines: dict[str, AsyncEngine] = {}
_makers: dict[str, async_sessionmaker[AsyncSession]] = {}


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python, whatever the backend keeps (SQLite drops the zone)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: use burnrate.common.context.now()")
        return value.astimezone(dt.UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def make_base(service: str) -> type[DeclarativeBase]:
    """Declarative base with its own metadata; includes the outbox and the processed-events tables."""
    if service in _bases:
        return _bases[service]

    class Base(DeclarativeBase):
        metadata = MetaData()
        type_annotation_map = {dict: JSON, list: JSON, dt.datetime: UTCDateTime}  # noqa: RUF012 - read by SQLAlchemy

    class Outbox(Base):
        __tablename__ = "outbox"
        id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
        topic: Mapped[str] = mapped_column(String(80), index=True)
        key: Mapped[str] = mapped_column(String(80), default="")
        payload: Mapped[dict] = mapped_column(JSON)
        headers: Mapped[dict] = mapped_column(JSON, default=dict)
        created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
        published_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
        attempts: Mapped[int] = mapped_column(default=0)

    class Processed(Base):
        """Idempotent consumers: an event id is handled once per consumer group."""

        __tablename__ = "processed_events"
        consumer: Mapped[str] = mapped_column(String(80), primary_key=True)
        event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
        at: Mapped[dt.datetime] = mapped_column(UTCDateTime)

    Base.Outbox = Outbox  # type: ignore[attr-defined]
    Base.Processed = Processed  # type: ignore[attr-defined]
    _bases[service] = Base
    return Base


def base_of(service: str) -> type[DeclarativeBase]:
    return _bases[service]


def known_services() -> list[str]:
    return list(_bases)


def _url(service: str) -> str:
    st = settings()
    name = service.replace("-", "_")
    if st.database_url:
        return st.database_url.replace("{service}", name)
    os.makedirs(st.data_dir, exist_ok=True)
    return f"sqlite+aiosqlite:///{os.path.join(st.data_dir, name)}.db"


def engine(service: str) -> AsyncEngine:
    if service not in _engines:
        url = _url(service)
        if url.startswith("sqlite"):
            eng = create_async_engine(url, connect_args={"timeout": 15})

            @event.listens_for(eng.sync_engine, "connect")
            def _pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.close()
        else:
            eng = create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
        _engines[service] = eng
        _makers[service] = async_sessionmaker(eng, expire_on_commit=False)
    return _engines[service]


@asynccontextmanager
async def session(service: str) -> AsyncIterator[AsyncSession]:
    """Transaction scope: commits on success, rolls back on error."""
    engine(service)
    async with _makers[service]() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


async def create_all(service: str) -> None:
    async with engine(service).begin() as conn:
        await conn.run_sync(_bases[service].metadata.create_all)


async def dispose_all() -> None:
    for eng in _engines.values():
        await eng.dispose()
    _engines.clear()
    _makers.clear()
