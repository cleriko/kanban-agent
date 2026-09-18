from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings, get_settings
from .models import Base

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        kwargs: dict = {"echo": False, "future": True}
        if not settings.is_sqlite:
            # Modest pool: this is one user on one VPS, not a web fleet.
            kwargs.update(pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine


def session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(engine(settings), expire_on_commit=False, class_=AsyncSession)
    return _factory


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Transaction per unit of work. Commits on success, rolls back on any exception."""
    async with session_factory(settings)() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def create_all() -> None:
    """Used by tests and first run. Production uses Alembic migrations."""
    async with engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
