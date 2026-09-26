"""Conexão com o PostgreSQL e *lock* exclusivo de instância."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

AGENT_LOCK_KEY = 0x7A_61_67_65_6E_74  # "zagent": chave do pg_advisory_lock da instância


class AlreadyRunningError(RuntimeError):
    """Outra instância do agente já detém o *lock* exclusivo."""


class Database:
    """Motor assíncrono, fábrica de sessões e *lock* de instância única."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Sessão transacional: *commit* ao sair sem erro, *rollback* em exceção."""
        async with self.sessions() as session, session.begin():
            yield session

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            return bool(await conn.scalar(text("SELECT 1")))

    @asynccontextmanager
    async def exclusive_lock(self, key: int = AGENT_LOCK_KEY) -> AsyncIterator[None]:
        """Garante uma única instância ativa (``pg_try_advisory_lock``).

        O *lock* pertence à conexão: se o processo morrer, o PostgreSQL o libera sozinho.
        """
        conn = await self.engine.connect()
        try:
            acquired = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)").bindparams(key=key)
            )
            if not acquired:
                raise AlreadyRunningError("outra instância do agente já está em execução")
            try:
                yield
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)").bindparams(key=key))
        finally:
            await conn.close()
