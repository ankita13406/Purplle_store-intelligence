"""
database.py — Async SQLAlchemy engine + table definitions
Defaults to SQLite for local dev; set DATABASE_URL env var for PostgreSQL.
"""
import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import (
    String, Boolean, Float, Integer, Text, Index,
    func, text
)
from datetime import datetime
from typing import Optional, AsyncGenerator

log = logging.getLogger("db")

_RAW = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./store_intelligence.db")

# Translate bare postgres:// → postgresql+asyncpg://
if _RAW.startswith("postgresql://") or _RAW.startswith("postgres://"):
    DATABASE_URL = _RAW.replace("postgresql://", "postgresql+asyncpg://", 1)\
                        .replace("postgres://",   "postgresql+asyncpg://", 1)
elif _RAW.startswith("sqlite://"):
    DATABASE_URL = _RAW.replace("sqlite://", "sqlite+aiosqlite://", 1)
else:
    DATABASE_URL = _RAW

IS_SQLITE = "sqlite" in DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    **({} if IS_SQLITE else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
    }),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "events"

    id:          Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id:    Mapped[str]           = mapped_column(String(64),  unique=True, nullable=False, index=True)
    store_id:    Mapped[str]           = mapped_column(String(32),  nullable=False, index=True)
    camera_id:   Mapped[str]           = mapped_column(String(32),  nullable=False)
    visitor_id:  Mapped[str]           = mapped_column(String(32),  nullable=False, index=True)
    event_type:  Mapped[str]           = mapped_column(String(32),  nullable=False, index=True)
    timestamp:   Mapped[str]           = mapped_column(String(25),  nullable=False, index=True)
    zone_id:     Mapped[Optional[str]] = mapped_column(String(64),  nullable=True,  index=True)
    dwell_ms:    Mapped[int]           = mapped_column(Integer,     default=0)
    is_staff:    Mapped[bool]          = mapped_column(Boolean,     default=False)
    confidence:  Mapped[float]         = mapped_column(Float,       default=1.0)
    queue_depth: Mapped[Optional[int]] = mapped_column(Integer,     nullable=True)
    sku_zone:    Mapped[Optional[str]] = mapped_column(String(64),  nullable=True)
    session_seq: Mapped[Optional[int]] = mapped_column(Integer,     nullable=True)
    ingested_at: Mapped[str]           = mapped_column(String(25),  nullable=False)

    __table_args__ = (
        Index("ix_events_store_ts",    "store_id", "timestamp"),
        Index("ix_events_store_type",  "store_id", "event_type"),
        Index("ix_events_visitor",     "store_id", "visitor_id"),
    )


class DailyMetricsCache(Base):
    """Optional pre-aggregated daily cache — updated on ingest."""
    __tablename__ = "daily_metrics_cache"

    id:               Mapped[int]  = mapped_column(Integer, primary_key=True)
    store_id:         Mapped[str]  = mapped_column(String(32), nullable=False)
    date:             Mapped[str]  = mapped_column(String(10), nullable=False)   # YYYY-MM-DD
    unique_visitors:  Mapped[int]  = mapped_column(Integer, default=0)
    total_entries:    Mapped[int]  = mapped_column(Integer, default=0)
    total_exits:      Mapped[int]  = mapped_column(Integer, default=0)
    purchases:        Mapped[int]  = mapped_column(Integer, default=0)
    updated_at:       Mapped[str]  = mapped_column(String(25), nullable=False)

    __table_args__ = (
        Index("ix_daily_store_date", "store_id", "date", unique=True),
    )


# ---------------------------------------------------------------------------
# Dependency + lifecycle
# ---------------------------------------------------------------------------

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database initialised: %s", DATABASE_URL.split("@")[-1])


async def check_db() -> bool:
    """Liveness check — returns True if DB is reachable."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.error("DB health check failed: %s", exc)
        return False
