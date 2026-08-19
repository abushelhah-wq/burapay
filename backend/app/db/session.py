"""
Async engine and session factory.

PostgreSQL via asyncpg in every real deployment. The test suite points
``DATABASE_URL`` at ``sqlite+aiosqlite``, which the models are written to tolerate:
no server-side defaults, no native enums, no PostgreSQL-only column types.
"""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession, async_sessionmaker,
                                    create_async_engine)

from app.core.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = settings.database_url
        kwargs: dict = {"echo": False, "future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # SQLite has no pool to pre-ping and rejects the pool arguments below.
            # The busy timeout matters: the benchmark engine writes from a background
            # task while a request reads, and SQLite serialises writers.
            kwargs = {"echo": False, "future": True,
                      "connect_args": {"timeout": 30}}
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False,
            autoflush=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
