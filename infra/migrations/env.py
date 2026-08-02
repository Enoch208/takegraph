"""Alembic environment.

The database URL comes from DATABASE_URL at runtime, never from alembic.ini, so
no credential is committed (PRD §19.6). Migrations run synchronously against a
psycopg driver even though the application uses asyncpg — Alembic's own
machinery is sync, and mixing the two buys nothing here.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from takegraph_api.db import models  # noqa: F401 — imported for metadata registration
from takegraph_api.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Migrations refuse to guess a connection string; "
            "copy .env.example to .env or export it explicitly."
        )
    # The app talks asyncpg; Alembic needs a sync driver.
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg://")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
