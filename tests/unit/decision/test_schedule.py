import asyncio
from datetime import UTC, datetime, timedelta

from trade_agent.decision.schedule import next_candle_close, run_on_candle_close

CLOSE = datetime(2026, 9, 26, 16, tzinfo=UTC)


def test_next_candle_close() -> None:
    at = datetime(2026, 9, 26, 13, 59, 59, tzinfo=UTC)
    assert next_candle_close("4h", at) == CLOSE
    assert next_candle_close("1h", at) == datetime(2026, 9, 26, 14, tzinfo=UTC)
    assert next_candle_close("4h", CLOSE) == datetime(2026, 9, 26, 20, tzinfo=UTC)
    assert next_candle_close("1d", CLOSE) == datetime(2026, 9, 27, tzinfo=UTC)


async def _runs(now: datetime, delay: timedelta, *, stop_early: bool = False) -> int:
    stop = asyncio.Event()
    runs: list[int] = []

    async def action() -> None:
        runs.append(1)
        stop.set()

    task = asyncio.create_task(
        run_on_candle_close("4h", stop, action, delay=delay, clock=lambda: now)
    )
    if stop_early:
        await asyncio.sleep(0.05)
        stop.set()
    await asyncio.wait_for(task, timeout=5)
    return len(runs)


async def test_runs_after_the_close_plus_delay() -> None:
    assert await _runs(CLOSE, timedelta(milliseconds=10)) == 1


async def test_waking_inside_the_delay_does_not_skip_the_cycle() -> None:
    # woke up 5 ms after the close, before the 10 ms delay: runs this cycle
    assert await _runs(CLOSE + timedelta(milliseconds=5), timedelta(milliseconds=10)) == 1


async def test_stop_interrupts_the_wait() -> None:
    # just ran: the next cycle is only at the following close (4h)
    delay = timedelta(seconds=20)
    assert await _runs(CLOSE + delay, delay, stop_early=True) == 0
