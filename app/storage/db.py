"""Async database engine and session factory.

SQLite (aiosqlite) for local dev and tests, Postgres (asyncpg) for anything shared.
The code is identical for both: the URL decides.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        kwargs = {}
        if url.startswith("postgresql"):
            # Sensible pool defaults for a web + worker deployment; tune per instance size.
            kwargs = {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Create tables if missing. Fine for dev; use Alembic migrations in prod."""
        from app.enterprise import rbac  # noqa: F401  (api_keys, audit_log)
        from app.personalization import profiles  # noqa: F401  (customer_profiles)
        from app.privacy import vault  # noqa: F401  (pii_tokens table)
        from app.storage import models  # noqa: F401  (register models on Base.metadata)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
