"""Scheduling at the candle close (the same cadence validated in the lab)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from trade_agent.market.candles import INTERVAL_MS

log = structlog.get_logger(__name__)

POLL = timedelta(seconds=30)
"""How often the wait reads the wall clock. A timer alone would not do: asyncio's clock stops
while the host sleeps, so a wait resumes where it stopped, at no particular point of a candle."""
WAKE_SETTLE = timedelta(seconds=60)
"""The wait before a late cycle: DNS and the network take a few seconds to return after a sleep,
and a cycle failing there would cost its exits and count against the API error rate."""


def next_candle_close(timeframe: str, now: datetime) -> datetime:
    """Next candle close of the ``timeframe`` (UTC), strictly after ``now``."""
    interval_ms = INTERVAL_MS[timeframe]
    now_ms = int(now.timestamp() * 1000)
    close_ms = (now_ms // interval_ms + 1) * interval_ms
    return datetime.fromtimestamp(close_ms / 1000, UTC)


def last_candle_close(timeframe: str, now: datetime) -> datetime:
    """Close of the last closed candle of the ``timeframe`` (UTC), at or before ``now``."""
    interval_ms = INTERVAL_MS[timeframe]
    close_ms = int(now.timestamp() * 1000) // interval_ms * interval_ms
    return datetime.fromtimestamp(close_ms / 1000, UTC)


async def _stopped_within(stop: asyncio.Event, timeout_s: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_s)
    except TimeoutError:
        return False
    return True


async def run_on_candle_close(
    timeframe: str,
    stop: asyncio.Event,
    action: Callable[[], Awaitable[object]],
    *,
    delay: timedelta = timedelta(seconds=20),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    poll: timedelta = POLL,
    settle: timedelta = WAKE_SETTLE,
) -> None:
    """Runs ``action`` right after each close (``delay`` lets the candle settle).

    Closes missed while the host slept become one late run, shortly after it wakes: the exits
    should not wait for the next close. The engine refuses entries on that run (D-051).
    """
    while not stop.is_set():
        now = clock()
        # from (now - delay): waking up between the close and the delay does not skip the cycle
        target = next_candle_close(timeframe, now - delay) + delay
        log.debug(
            "schedule.next_run",
            timeframe=timeframe,
            at=target.isoformat(),
            wait_s=round((target - now).total_seconds()),
        )
        while (remaining := (target - clock()).total_seconds()) > 0:
            if await _stopped_within(stop, min(remaining, poll.total_seconds())):
                return
        overdue = clock() - target
        if overdue > poll:
            log.warning(
                "schedule.late",
                timeframe=timeframe,
                at=target.isoformat(),
                overdue_s=round(overdue.total_seconds()),
            )
            if await _stopped_within(stop, settle.total_seconds()):
                return
        await action()
