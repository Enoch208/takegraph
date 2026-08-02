"""Real-PostgreSQL worker integration fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DEFAULT_URL = "postgresql+asyncpg://takegraph:takegraph_local@127.0.0.1:5434/takegraph"
DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_URL


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — explicit integration skip
        await engine.dispose()
        pytest.skip(f"PostgreSQL unreachable at {DATABASE_URL.split('@')[-1]}: {exc}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as current:
        await current.execute(
            text("truncate table work_items, b2_object_events, b2_webhook_messages, upload_intents")
        )
        await current.commit()
        yield current
        await current.rollback()
        await current.execute(
            text("truncate table work_items, b2_object_events, b2_webhook_messages, upload_intents")
        )
        await current.commit()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
