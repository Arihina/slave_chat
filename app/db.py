from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine() -> AsyncEngine:
    global _engine, _session_factory
    _engine = create_async_engine(
        settings.db_url,
        echo=False,
        pool_pre_ping=True,
        max_overflow=0,
    )
    _session_factory = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Сессионный фактори не инициализирован")
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = session_factory()
    async with factory() as session:
        yield session
