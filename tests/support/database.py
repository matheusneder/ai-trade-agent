"""PostgreSQL in a container (testcontainers) for the integration tests.

``TA_TEST_POSTGRES_SERVER`` (the URL of an ephemeral server's maintenance database, set by the
mutation tests, see tests/mutation/README.md) gives each session a new database on that server
instead of a container of its own: the mutation tests run thousands of short sessions.
"""

import asyncio
import os
import secrets
import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support.logs import image
from trade_agent.persistence.db import Database
from trade_agent.persistence.migrate import upgrade_to_head

TABLES = (
    "positions, intents, exchange_orders, fills, events, checkpoints, "
    "news_items, research_reports, llm_usage"
)
SERVER_ENV = "TA_TEST_POSTGRES_SERVER"


async def _migrate(url: str) -> None:
    engine = create_async_engine(url)
    await upgrade_to_head(engine)
    await engine.dispose()


async def _create_or_drop(server: str, statement: str) -> None:
    engine = create_async_engine(server, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _database_on(server: str) -> Iterator[str]:
    """A new, migrated database on ``server`` for this session, dropped at the end."""
    name = f"ta_test_{os.getpid()}_{secrets.token_hex(4)}"
    asyncio.run(_create_or_drop(server, f'CREATE DATABASE "{name}"'))
    try:
        url = make_url(server).set(database=name).render_as_string(hide_password=False)
        asyncio.run(_migrate(url))
        yield url
    finally:
        asyncio.run(_create_or_drop(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def postgres_container_url() -> Iterator[str]:
    if server := os.environ.get(SERVER_ENV):
        yield from _database_on(server)
        return
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível: testes com PostgreSQL ignorados")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(image("postgres"), driver="asyncpg") as container:
        url = container.get_connection_url()
        asyncio.run(_migrate(url))
        yield url


async def fresh_database(url: str) -> AsyncIterator[Database]:
    database = Database(url)
    try:
        yield database
    finally:
        async with database.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await database.dispose()


async def terminate_lock_holder(db: Database, key: int) -> None:
    """What PostgreSQL does to every session when it stops, only to the lock's session."""
    async with db.engine.connect() as admin:
        await admin.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_locks"
                " WHERE locktype = 'advisory' AND objid = :low AND classid = :high"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            ).bindparams(low=key & 0xFFFFFFFF, high=key >> 32)
        )
        await admin.commit()
