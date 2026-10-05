"""Conexão com o PostgreSQL e *lock* exclusivo de instância.

Cada comando SQL vira um span do componente ``db`` (texto do comando com os marcadores
``$1``, ``$2``...; os valores nunca vão para o span).
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from opentelemetry.trace import SpanKind
from sqlalchemy import event, text
from sqlalchemy.engine import Engine, ExceptionContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from trade_agent import tracing

AGENT_LOCK_KEY = 0x7A_61_67_65_6E_74  # "zagent": chave do pg_advisory_lock da instância
MAX_QUERY_TEXT = 2000
_TARGET = re.compile(r"\b(?:FROM|INTO|UPDATE|TRUNCATE|JOIN)\s+([a-z_][\w.]*)", re.IGNORECASE)
_SPAN_ATTR = "_trade_agent_span"


def statement_span_name(statement: str) -> tuple[str, str | None]:
    """Nome do span (``SELECT positions``) e a tabela principal, se houver."""
    operation = statement.split(maxsplit=1)[0].upper() if statement.strip() else "SQL"
    target = _TARGET.search(statement)
    table = target.group(1) if target else None
    return (f"{operation} {table}" if table else operation), table


def _start_span(
    conn: Any, _cursor: Any, statement: str, _params: Any, context: Any, _many: bool
) -> None:
    name, table = statement_span_name(statement)
    attributes: dict[str, tracing.AttributeValue] = {
        "db.system.name": "postgresql",
        "db.operation.name": name.split()[0],
        "db.query.text": statement[:MAX_QUERY_TEXT],
        "server.address": conn.engine.url.host or "",
        "peer.service": "postgres",
    }
    if table:
        attributes["db.collection.name"] = table
    span = tracing.tracer("db").start_span(name, kind=SpanKind.CLIENT, attributes=attributes)
    setattr(context, _SPAN_ATTR, span)


def _end_span(
    _conn: Any, _cursor: Any, _stmt: str, _params: Any, context: Any, _many: bool
) -> None:
    span = getattr(context, _SPAN_ATTR, None)
    if span is not None:
        span.end()


def _fail_span(error: ExceptionContext) -> None:
    # sem contexto de execução (ex.: falha ao conectar) não há span de comando aberto
    span = getattr(error.execution_context, _SPAN_ATTR, None)
    if span is not None:
        original = error.original_exception
        tracing.fail(span, original)
        span.set_attribute("error.type", type(original).__name__)
        sqlstate = getattr(original, "sqlstate", None)
        if sqlstate:
            span.set_attribute("db.response.status_code", sqlstate)
        span.end()


def trace_statements(engine: Engine) -> None:
    """Liga spans de comando SQL ao motor (o span pai vem do contexto da tarefa)."""
    event.listen(engine, "before_cursor_execute", _start_span)
    event.listen(engine, "after_cursor_execute", _end_span)
    event.listen(engine, "handle_error", _fail_span)


class AlreadyRunningError(RuntimeError):
    """Outra instância do agente já detém o *lock* exclusivo."""


class Database:
    """Motor assíncrono, fábrica de sessões e *lock* de instância única."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        trace_statements(self.engine.sync_engine)

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
        A conexão fica sem transação (*autocommit*): aberta durante toda a execução do
        agente, uma transação prenderia o horizonte do VACUUM, e as linhas mortas de todas
        as tabelas ficariam sem limpeza.
        """
        conn = await self.engine.connect()
        try:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
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
