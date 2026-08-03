"""Async engine and session factory.

One engine per process. Sessions are short-lived and scoped to a unit of work —
§8.5 defines the transaction boundaries the application must respect, and holding
a session open across an external provider call would pin a connection for
minutes at a time.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. The application refuses to guess a connection "
            "string; copy .env.example to .env or export it explicitly."
        )
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            database_url(),
            pool_pre_ping=True,
            # Anything between this process and Postgres that silently drops idle
            # TCP — Docker Desktop's port proxy, a NAT gateway, a load balancer —
            # leaves the pool holding sockets that are open here and gone there.
            # pool_pre_ping alone does not save us: the ping is sent on the dead
            # socket and hangs until command_timeout, and SQLAlchemy does not
            # classify a TimeoutError as a disconnect, so it propagates to the
            # caller. Recycling well inside the usual idle-drop window means the
            # connection is replaced before it can go stale.
            pool_recycle=300,
            # §20.2: every boundary has a timeout. No statement runs unbounded.
            #
            # 60s, not 15s. The queries here are indexed and finish in
            # milliseconds; what blows the budget is the server stalling, and a
            # Postgres checkpoint on Docker Desktop's filesystem has been observed
            # taking over 90 seconds to fsync. At 15s that surfaced as work items
            # failing and a worker dying on a pre-ping, neither of which describes
            # anything actually wrong with the query.
            connect_args={"command_timeout": 60},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transactional unit of work. Commits on success, rolls back on any
    exception — a partially applied state transition is never left behind."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
