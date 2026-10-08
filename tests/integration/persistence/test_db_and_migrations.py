import asyncio
import io
from contextlib import redirect_stdout

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from trade_agent.persistence.db import AlreadyRunningError, Database
from trade_agent.persistence.migrate import alembic_config, downgrade_to_base, upgrade_to_head
from trade_agent.persistence.models import Base


async def _insert_then_fail(db: Database) -> None:
    async with db.session() as session:
        await session.execute(text("INSERT INTO checkpoints (key, value) VALUES ('x', '{}')"))
        raise RuntimeError("falha no meio da transação")


async def test_ping_and_session_rollback(db: Database) -> None:
    assert await db.ping()
    with pytest.raises(RuntimeError, match="falha no meio"):
        await _insert_then_fail(db)
    async with db.session() as session:
        assert await session.scalar(text("SELECT count(*) FROM checkpoints")) == 0


async def test_exclusive_lock_allows_a_single_instance(db: Database, postgres_url: str) -> None:
    other = Database(postgres_url)
    try:
        async with db.exclusive_lock(key=4242):
            with pytest.raises(AlreadyRunningError):
                async with other.exclusive_lock(key=4242):
                    pass
        async with other.exclusive_lock(key=4242):
            pass
    finally:
        await other.dispose()


async def test_the_instance_lock_leaves_no_open_transaction(db: Database) -> None:
    """With an open transaction, the lock's connection would pin the VACUUM horizon (the
    ``backend_xmin``) while the agent runs, and the dead rows would not be removed."""
    async with db.exclusive_lock(key=4243), db.session() as session:
        holder = await session.execute(
            text(
                "SELECT a.state, a.backend_xmin FROM pg_locks l JOIN pg_stat_activity a"
                " USING (pid) WHERE l.locktype = 'advisory' AND l.objid = 4243"
            )
        )
        assert holder.one() == ("idle", None)


async def test_pool_recovers_after_connection_is_killed(db: Database) -> None:
    assert await db.ping()
    async with db.engine.connect() as admin:
        await admin.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
        await admin.commit()
    assert await db.ping()  # pool_pre_ping discards the dead connection


async def test_models_match_migrations(db: Database) -> None:
    async with db.engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    assert diff == []


async def test_downgrade_and_upgrade_roundtrip(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    try:
        await downgrade_to_base(engine)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('public.positions')")) is None
        await upgrade_to_head(engine)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('public.positions')")) == "positions"
    finally:
        await engine.dispose()


def test_offline_sql_generation(postgres_url: str) -> None:
    config = alembic_config()
    config.cmd_opts = type("Opts", (), {"x": [f"url={postgres_url}"]})()
    buffer = io.StringIO()
    config.stdout = buffer
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    assert "CREATE TABLE positions" in buffer.getvalue()


def test_env_uses_database_url_without_injected_connection(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TA_DATABASE_URL", postgres_url)
    command.upgrade(alembic_config(), "head")  # already at head: runs the asynchronous path
    engine = create_async_engine(postgres_url)

    async def current() -> str | None:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        await engine.dispose()
        return str(version) if version is not None else None

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert asyncio.run(current()) == head


def test_env_requires_url() -> None:
    with pytest.raises(RuntimeError, match="TA_DATABASE_URL"):
        command.upgrade(alembic_config(), "head")
