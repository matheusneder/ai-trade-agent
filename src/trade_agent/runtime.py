"""Agent lifecycle.

Exclusive *lock*, migrations, startup recovery, periodic reconciliation, periodic clock
re-measurement, *heartbeat*, reaction to User Data Stream events and the background tasks
the application builds: periodic ones (risk, news collection), at the candle close (decision
cycle per profile) and long-running services (Telegram commands), supervised.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry.trace import INVALID_SPAN

from trade_agent import tracing
from trade_agent.decision.schedule import run_on_candle_close
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
from trade_agent.reconcile.reconciler import Reconciler, ReconcileReport

log = structlog.get_logger(__name__)

type EventSource = Callable[[], AsyncIterator[UserEvent]]
type Action = Callable[[], Awaitable[object]]
type Service = Callable[[asyncio.Event], Awaitable[None]]


def job_name(action: Action) -> str:
    """Short task name (``check_risk``, ``reconcile``, ``decide_conservador``)."""
    qualname = getattr(action, "__qualname__", type(action).__name__)
    return str(qualname).rsplit(".", 1)[-1].lstrip("_")


def affected_decision(event: UserEvent) -> str | None:
    """Agent decision affected by an order/list event (``None`` for third parties)."""
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
        clock_sync_interval_s: float = 600.0,
        heartbeat: Action | None = None,
        heartbeat_interval_s: float = 60.0,
        periodic: Sequence[tuple[float, Action]] = (),
        candle_jobs: Sequence[tuple[str, Action]] = (),
        services: Sequence[Service] = (),
        on_reconcile: Callable[[ReconcileReport], Awaitable[None]] | None = None,
        candle_delay: timedelta = timedelta(seconds=20),
        service_backoff_s: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.api = api
        self.store = store
        self.reconciler = reconciler
        self._events = events
        self._reconcile_interval_s = reconcile_interval_s
        self._clock_sync_interval_s = clock_sync_interval_s
        self._heartbeat = heartbeat
        self._heartbeat_interval_s = heartbeat_interval_s
        self._periodic = list(periodic)
        self._candle_jobs = list(candle_jobs)
        self._services = list(services)
        self._on_reconcile = on_reconcile
        self._candle_delay = candle_delay
        self._service_backoff_s = service_backoff_s
        self._clock = clock

    async def run(self, stop: asyncio.Event) -> None:
        """Runs until ``stop``; raises ``AlreadyRunningError`` if another instance exists."""
        async with self.db.exclusive_lock():
            with tracing.span("runtime", "agent.start"):
                await upgrade_to_head(self.db.engine)
                offset = await self.api.rest.sync_time()
                report = await self._reconcile()
                await self.store.record_event(
                    "agent.started",
                    Severity.INFO,
                    {"clock_offset_ms": offset, "reconcile": report.as_dict()},
                )
                tracing.annotate(clock_offset_ms=offset, positions=report.positions)
                log.info("agent.started", positions=report.positions, clock_offset_ms=offset)
            # reconciliation has just run; the other tasks already run at startup
            # (without waiting a whole interval with no risk, telemetry or news)
            jobs: list[tuple[float, Action, bool, bool]] = [
                (self._reconcile_interval_s, self._reconcile, False, True),
                # the local clock may jump while the agent runs (NTP turned back on, a VM that
                # woke up): without a new measurement, signed requests would wait for a -1021
                (self._clock_sync_interval_s, self.api.rest.sync_time, False, True),
            ]
            if self._heartbeat is not None:  # one ping per minute: no trace
                jobs.append((self._heartbeat_interval_s, self._heartbeat, True, False))
            jobs += [(interval, action, True, True) for interval, action in self._periodic]
            periodic = [
                asyncio.create_task(
                    self._every(interval, stop, action, immediate=immediate, trace=trace)
                )
                for interval, action, immediate, trace in jobs
            ]
            periodic += [
                asyncio.create_task(
                    run_on_candle_close(
                        timeframe,
                        stop,
                        self._guarded_action(action),
                        delay=self._candle_delay,
                        clock=self._clock,
                    )
                )
                for timeframe, action in self._candle_jobs
            ]
            periodic += [
                asyncio.create_task(self._supervise(service, stop)) for service in self._services
            ]
            consumer = asyncio.create_task(self._consume(self._events)) if self._events else None
            log.debug(
                "runtime.tasks_started",
                periodic=[(interval, job_name(action)) for interval, action, _, _ in jobs],
                candle_jobs=[timeframe for timeframe, _ in self._candle_jobs],
                services=len(self._services),
                user_stream=consumer is not None,
            )
            try:
                await stop.wait()
            finally:
                stop.set()  # the periodic tasks finish the current cycle and exit
                if consumer is not None:
                    consumer.cancel()
                await asyncio.gather(
                    *periodic, *([consumer] if consumer else []), return_exceptions=True
                )
                await self.store.record_event("agent.stopped", Severity.INFO)
                log.info("agent.stopped")

    async def _reconcile(self) -> ReconcileReport:
        report = await self.reconciler.reconcile_all()
        if self._on_reconcile is not None:
            await self._on_reconcile(report)
        return report

    def _guarded_action(self, action: Action) -> Action:
        async def guarded() -> object:
            await self._guarded(action)
            return None

        return guarded

    async def _supervise(self, service: Service, stop: asyncio.Event) -> None:
        """Keeps a long-running service alive: failures are recorded and it restarts."""
        while not stop.is_set():
            try:
                await service(stop)
            except Exception as exc:
                log.error("runtime.service_failed", error=repr(exc))
                with contextlib.suppress(Exception):
                    await self.store.record_event(
                        "runtime.service_failed", Severity.HIGH, {"error": repr(exc)}
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._service_backoff_s)

    async def _every(
        self,
        interval_s: float,
        stop: asyncio.Event,
        action: Action,
        *,
        immediate: bool = False,
        trace: bool = True,
    ) -> None:
        if immediate:
            await self._guarded(action, trace=trace)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                await self._guarded(action, trace=trace)

    async def _guarded(self, action: Action, *, trace: bool = True) -> None:
        """Runs a background task (the root of a trace) without letting a failure propagate."""
        name = job_name(action)
        started = time.monotonic()
        scope = (
            tracing.span("runtime", f"job {name}")
            if trace
            else contextlib.nullcontext(INVALID_SPAN)
        )
        with scope as span:
            try:
                await action()
                log.debug(
                    "runtime.job", job=name, elapsed_ms=round((time.monotonic() - started) * 1000)
                )
            except Exception as exc:  # background tasks must never die
                tracing.fail(span, exc)
                log.error("runtime.task_failed", job=name, error=repr(exc))
                with contextlib.suppress(Exception):
                    await self.store.record_event(
                        "runtime.task_failed", Severity.HIGH, {"job": name, "error": repr(exc)}
                    )

    async def _consume(self, events: EventSource) -> None:
        async for event in events():
            if isinstance(event, StreamConnected):
                if event.reconnected:
                    await self._guarded(self._reconcile)
                continue
            decision = affected_decision(event)
            if decision is not None:
                await self._guarded(self._reconcile_decision_action(decision))

    def _reconcile_decision_action(self, decision: str) -> Action:
        async def reconcile_decision() -> object:
            return await self.reconciler.reconcile_decision(decision)

        return reconcile_decision
