"""Scheduling at the candle close (the same cadence validated in the lab)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from trade_agent.market.candles import INTERVAL_MS

log = structlog.get_logger(__name__)


def next_candle_close(timeframe: str, now: datetime) -> datetime:
    """Next candle close of the ``timeframe`` (UTC), strictly after ``now``."""
    interval_ms = INTERVAL_MS[timeframe]
    now_ms = int(now.timestamp() * 1000)
    close_ms = (now_ms // interval_ms + 1) * interval_ms
    return datetime.fromtimestamp(close_ms / 1000, UTC)


async def run_on_candle_close(
    timeframe: str,
    stop: asyncio.Event,
    action: Callable[[], Awaitable[object]],
    *,
    delay: timedelta = timedelta(seconds=20),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Runs ``action`` right after each close (``delay`` lets the candle settle)."""
    while not stop.is_set():
        now = clock()
        # from (now - delay): waking up between the close and the delay does not skip the cycle
        target = next_candle_close(timeframe, now - delay) + delay
        wait_s = (target - now).total_seconds()
        log.debug(
            "schedule.next_run", timeframe=timeframe, at=target.isoformat(), wait_s=round(wait_s)
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(wait_s, 0))
        except TimeoutError:
            await action()
