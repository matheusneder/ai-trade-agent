"""Alembic environment (asynchronous; accepts a connection injected via ``config.attributes``)."""

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from trade_agent.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get("TA_DATABASE_URL")
    if not url:
        raise RuntimeError("defina TA_DATABASE_URL ou use `alembic -x url=...`")
    return url


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    injected = config.attributes.get("connection")
    if injected is not None:
        _run(injected)
    else:
        asyncio.run(_run_async())
