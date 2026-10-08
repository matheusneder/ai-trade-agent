"""PostgreSQL connection and the exclusive instance *lock*.

Every SQL statement becomes a span of the ``db`` component (statement text with the
``$1``, ``$2``... placeholders; the values never go into the span).
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

AGENT_LOCK_KEY = 0x7A_61_67_65_6E_74  # "zagent": key of the instance's pg_advisory_lock
MAX_QUERY_TEXT = 2000
_TARGET = re.compile(r"\b(?:FROM|INTO|UPDATE|TRUNCATE|JOIN)\s+([a-z_][\w.]*)", re.IGNORECASE)
_SPAN_ATTR = "_trade_agent_span"


def statement_span_name(statement: str) -> tuple[str, str | None]:
    """Span name (``SELECT positions``) and the main table, if any."""
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
    # without an execution context (e.g. a failure to connect) there is no open statement span
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
    """Attaches SQL statement spans to the engine (the parent span comes from the task context)."""
    event.listen(engine, "before_cursor_execute", _start_span)
    event.listen(engine, "after_cursor_execute", _end_span)
    event.listen(engine, "handle_error", _fail_span)


class AlreadyRunningError(RuntimeError):
    """Another agent instance already holds the exclusive *lock*."""


class Database:
    """Asynchronous engine, session factory and single-instance *lock*."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        trace_statements(self.engine.sync_engine)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional session: *commit* on a clean exit, *rollback* on an exception."""
        async with self.sessions() as session, session.begin():
            yield session

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            return bool(await conn.scalar(text("SELECT 1")))

    @asynccontextmanager
    async def exclusive_lock(self, key: int = AGENT_LOCK_KEY) -> AsyncIterator[None]:
        """Ensures a single active instance (``pg_try_advisory_lock``).

        The *lock* belongs to the connection: if the process dies, PostgreSQL releases it on
        its own. The connection holds no transaction (*autocommit*): open for the agent's
        whole run, a transaction would pin the VACUUM horizon, and the dead rows of every
        table would never be cleaned up.
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
