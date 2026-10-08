"""Local clock off Binance's time (Windows without sync, a VM that jumps when it wakes up).

The simulated Binance validates ``timestamp``/``recvWindow`` like the real one. The agent
starts with the local clock 15 s behind and measures that offset; if the clock is corrected
while the agent runs, the offset measured at startup becomes 15 s wrong, and signed
requests would be refused (``-1021``) until the agent restarted.
"""

import asyncio
from collections.abc import Callable
from decimal import Decimal

from structlog.testing import capture_logs

from tests.support.clients import fake_api
from tests.support.fake_binance import FakeBinance
from trade_agent.execution.gateway import ExecutionGateway
from trade_agent.execution.service import PositionService, RulesCache
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Store
from trade_agent.reconcile.reconciler import Reconciler
from trade_agent.runtime import AgentRuntime

BEHIND_MS = 15_000  # the lag measured on Windows with the time service stopped


def _local_clock(fake: FakeBinance, skew: list[int]) -> Callable[[], int]:
    return lambda: fake.clock() + skew[0]


async def _until(condition: Callable[[], bool], timeout_s: float = 10) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():  # noqa: ASYNC110 - another component's state, no event
            await asyncio.sleep(0.005)


async def test_order_after_a_clock_correction_is_sent_once(fake: FakeBinance) -> None:
    skew = [-BEHIND_MS]
    async with fake_api(fake, clock=_local_clock(fake, skew)) as api:
        assert await api.rest.sync_time() == BEHIND_MS
        await api.account()  # the offset measured at startup holds
        skew[0] = 0  # Windows syncs again with the agent running
        with capture_logs() as logs:
            order = await api.new_order(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "0.001",
                    "newClientOrderId": "ta1-mod-c10c000000-0-E",
                }
            )
        assert api.rest.time_offset_ms == 0
    assert order.executed_qty == Decimal("0.001")
    assert len(fake.orders) == 1  # refused before executing: the retry does not duplicate
    events = [e["event"] for e in logs]
    assert events.count("rest.timestamp_rejected") == 1
    assert "rest.clock_jumped" in events


async def test_runtime_remeasures_the_clock_without_waiting_for_a_rejection(
    db: Database, store: Store, fake: FakeBinance
) -> None:
    skew = [-BEHIND_MS]
    async with fake_api(fake, clock=_local_clock(fake, skew)) as api:
        service = PositionService(api, ExecutionGateway(api), store, RulesCache(api))
        runtime = AgentRuntime(
            db=db,
            api=api,
            store=store,
            reconciler=Reconciler(api, service, store),
            reconcile_interval_s=3600,  # no signed request after startup
            clock_sync_interval_s=0.01,
        )
        stop = asyncio.Event()
        with capture_logs() as logs:
            task = asyncio.create_task(runtime.run(stop))
            await _until(lambda: any(e["event"] == "runtime.tasks_started" for e in logs))
            assert api.rest.time_offset_ms == BEHIND_MS
            skew[0] = 0
            await _until(lambda: api.rest.time_offset_ms == 0)
            stop.set()
            await asyncio.wait_for(task, timeout=10)
    events = [e["event"] for e in logs]
    assert "rest.clock_jumped" in events and "rest.timestamp_rejected" not in events
    assert any(e["event"] == "runtime.job" and e["job"] == "sync_time" for e in logs)
