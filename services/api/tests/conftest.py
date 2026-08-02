"""Integration test fixtures.

These run against a real PostgreSQL, not a fake. The behaviour under test —
`FOR UPDATE SKIP LOCKED`, lease expiry, partial indexes, unique-constraint
arbitration — exists only in the database, so an in-memory substitute would
verify nothing.

Requires the local stack: `make up`. Tests are skipped with a clear reason if the
database is unreachable rather than failing with a connection error that looks
like a code defect.
"""

from __future__ import annotations

import os
import uuid
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
    """Per-test engine using NullPool.

    A session-scoped engine binds its connection pool to the event loop that
    created it, while pytest-asyncio gives each test a fresh loop — the mismatch
    surfaces as "attached to a different loop". NullPool means no connection
    outlives the test that opened it, so there is nothing to leak across loops.
    Connection setup is milliseconds; correctness is worth more here than reuse.
    """
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — surfaced as a skip, with the reason
        await engine.dispose()
        pytest.skip(f"PostgreSQL unreachable at {DATABASE_URL.split('@')[-1]}: {exc}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A session whose work is visible to other sessions.

    Deliberately NOT wrapped in a rollback-per-test transaction: the concurrency
    tests need two independent connections to see each other's committed rows,
    which a shared outer transaction would hide. Cleanup is by truncation.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as s:
        await s.execute(text("truncate table work_items"))
        await s.commit()
        yield s
        await s.rollback()
        await s.execute(text("truncate table work_items"))
        await s.commit()


@pytest_asyncio.fixture
async def session_factory(engine):
    """For tests that need genuinely concurrent connections."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def target_id() -> uuid.UUID:
    return uuid.uuid4()
