"""PostgreSQL in a container (testcontainers) for the integration tests."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from trade_agent.persistence.db import Database
from trade_agent.persistence.migrate import upgrade_to_head

TABLES = (
    "positions, intents, exchange_orders, fills, events, checkpoints, "
    "news_items, research_reports, llm_usage"
)


async def _migrate(url: str) -> None:
    engine = create_async_engine(url)
    await upgrade_to_head(engine)
    await engine.dispose()


def postgres_container_url() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível: testes com PostgreSQL ignorados")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="asyncpg") as container:
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
