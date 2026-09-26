"""Ciclo de vida do agente.

*Lock* exclusivo, migrações, recuperação na partida, reconciliação periódica, *heartbeat*
e reação aos eventos do User Data Stream.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog

from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.user_stream import (
    ExecutionReport,
    ListStatusEvent,
    StreamConnected,
    UserEvent,
)
from trade_agent.execution.ids import parse_client_id
from trade_agent.persistence.db import Database
from trade_agent.persistence.migrate import upgrade_to_head
from trade_agent.persistence.store import Severity, Store
from trade_agent.reconcile.reconciler import Reconciler

log = structlog.get_logger(__name__)

type EventSource = Callable[[], AsyncIterator[UserEvent]]
type Action = Callable[[], Awaitable[object]]


def affected_decision(event: UserEvent) -> str | None:
    """Decisão do agente afetada por um evento de ordem/lista (``None`` para terceiros)."""
    if isinstance(event, ExecutionReport):
        client_id = event.client_order_id
    elif isinstance(event, ListStatusEvent):
        client_id = event.list_client_order_id
    else:
        return None
    parts = parse_client_id(client_id)
    return parts.decision if parts else None


class AgentRuntime:
    def __init__(
        self,
        *,
        db: Database,
        api: BinanceSpotApi,
        store: Store,
        reconciler: Reconciler,
        events: EventSource | None = None,
        reconcile_interval_s: float = 300.0,
        heartbeat: Action | None = None,
        heartbeat_interval_s: float = 60.0,
    ) -> None:
        self.db = db
        self.api = api
        self.store = store
        self.reconciler = reconciler
        self._events = events
        self._reconcile_interval_s = reconcile_interval_s
        self._heartbeat = heartbeat
        self._heartbeat_interval_s = heartbeat_interval_s

    async def run(self, stop: asyncio.Event) -> None:
        """Executa até ``stop``; levanta ``AlreadyRunningError`` se houver outra instância."""
        async with self.db.exclusive_lock():
            await upgrade_to_head(self.db.engine)
            offset = await self.api.rest.sync_time()
            report = await self.reconciler.reconcile_all()
            await self.store.record_event(
                "agent.started",
                Severity.INFO,
                {"clock_offset_ms": offset, "reconcile": report.as_dict()},
            )
            log.info("agent.started", positions=report.positions, clock_offset_ms=offset)
            periodic = [
                asyncio.create_task(
                    self._every(self._reconcile_interval_s, stop, self.reconciler.reconcile_all)
                )
            ]
            if self._heartbeat is not None:
                periodic.append(
                    asyncio.create_task(
                        self._every(self._heartbeat_interval_s, stop, self._heartbeat)
                    )
                )
            consumer = asyncio.create_task(self._consume(self._events)) if self._events else None
            try:
                await stop.wait()
            finally:
                stop.set()  # as tarefas periódicas terminam o ciclo atual e saem
                if consumer is not None:
                    consumer.cancel()
                await asyncio.gather(
                    *periodic, *([consumer] if consumer else []), return_exceptions=True
                )
                await self.store.record_event("agent.stopped", Severity.INFO)
                log.info("agent.stopped")

    async def _every(self, interval_s: float, stop: asyncio.Event, action: Action) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                await self._guarded(action)

    async def _guarded(self, action: Action) -> None:
        try:
            await action()
        except Exception as exc:  # as tarefas de fundo nunca podem morrer
            log.error("runtime.task_failed", error=repr(exc))
            with contextlib.suppress(Exception):
                await self.store.record_event(
                    "runtime.task_failed", Severity.HIGH, {"error": repr(exc)}
                )

    async def _consume(self, events: EventSource) -> None:
        async for event in events():
            if isinstance(event, StreamConnected):
                if event.reconnected:
                    await self._guarded(self.reconciler.reconcile_all)
                continue
            decision = affected_decision(event)
            if decision is not None:
                await self._guarded(self._reconcile_decision_action(decision))

    def _reconcile_decision_action(self, decision: str) -> Action:
        async def action() -> object:
            return await self.reconciler.reconcile_decision(decision)

        return action
