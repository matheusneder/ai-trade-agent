"""Aplicação programática das migrações Alembic (partida do agente e testes)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """Aplica todas as migrações pendentes."""
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: command.upgrade(alembic_config(sync), "head"))


async def downgrade_to_base(engine: AsyncEngine) -> None:
    """Reverte todas as migrações (usado em testes)."""
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: command.downgrade(alembic_config(sync), "base"))
