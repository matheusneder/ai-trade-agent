import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from structlog.testing import capture_logs

from trade_agent.decision.schedule import (
    last_candle_close,
    next_candle_close,
    run_on_candle_close,
)

CLOSE = datetime(2026, 9, 26, 16, tzinfo=UTC)


def _running_from(start: datetime) -> Callable[[], datetime]:
    """A wall clock that starts at ``start`` and runs at real speed."""
    began = time.monotonic()
    return lambda: start + timedelta(seconds=time.monotonic() - began)


def test_next_candle_close() -> None:
    at = datetime(2026, 9, 26, 13, 59, 59, tzinfo=UTC)
    assert next_candle_close("4h", at) == CLOSE
    assert next_candle_close("1h", at) == datetime(2026, 9, 26, 14, tzinfo=UTC)
    assert next_candle_close("4h", CLOSE) == datetime(2026, 9, 26, 20, tzinfo=UTC)
    assert next_candle_close("1d", CLOSE) == datetime(2026, 9, 27, tzinfo=UTC)


def test_last_candle_close() -> None:
    at = datetime(2026, 9, 26, 19, 59, 59, tzinfo=UTC)
    assert last_candle_close("4h", at) == CLOSE
    assert last_candle_close("1h", at) == datetime(2026, 9, 26, 19, tzinfo=UTC)
    assert last_candle_close("4h", CLOSE) == CLOSE


async def _runs(now: datetime, delay: timedelta, *, stop_early: bool = False) -> int:
    stop = asyncio.Event()
    runs: list[int] = []

    async def action() -> None:
        runs.append(1)
        stop.set()

    task = asyncio.create_task(
        run_on_candle_close("4h", stop, action, delay=delay, clock=_running_from(now))
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


async def test_on_time_run_logs_no_lateness() -> None:
    with capture_logs() as logs:
        assert await _runs(CLOSE, timedelta(milliseconds=10)) == 1
    assert "schedule.late" not in [entry["event"] for entry in logs]


async def test_a_sleep_past_the_close_makes_one_late_run_at_once() -> None:
    # asyncio's timers stop in a sleep, with a wait of hours still ahead: only the wall clock tells
    stop = asyncio.Event()
    now = [CLOSE - timedelta(hours=1)]
    wakes_at = CLOSE + timedelta(hours=7, minutes=44)  # two closes missed
    runs: list[datetime] = []
    ran = asyncio.Event()

    async def action() -> None:
        runs.append(now[0])
        ran.set()

    with capture_logs() as logs:
        task = asyncio.create_task(
            run_on_candle_close(
                "4h",
                stop,
                action,
                clock=lambda: now[0],
                poll=timedelta(milliseconds=5),
                settle=timedelta(0),
            )
        )
        await asyncio.sleep(0.02)
        assert runs == []
        now[0] = wakes_at
        await asyncio.wait_for(ran.wait(), timeout=5)
        await asyncio.sleep(0.02)  # the next wait reads the clock: no second run for this sleep
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert runs == [wakes_at]
    assert [e["overdue_s"] for e in logs if e["event"] == "schedule.late"] == [27820]


async def test_a_sleep_inside_the_candle_keeps_the_close() -> None:
    # woke up before the close: nothing was missed, so nothing runs ahead of it
    stop = asyncio.Event()
    now = [CLOSE - timedelta(hours=3)]
    runs: list[datetime] = []

    async def action() -> None:
        runs.append(now[0])
        stop.set()

    task = asyncio.create_task(
        run_on_candle_close(
            "4h", stop, action, clock=lambda: now[0], poll=timedelta(milliseconds=5)
        )
    )
    await asyncio.sleep(0.02)
    now[0] = CLOSE - timedelta(minutes=1)
    await asyncio.sleep(0.02)
    assert runs == []
    now[0] = CLOSE + timedelta(seconds=20)
    await asyncio.wait_for(task, timeout=5)
    assert runs == [CLOSE + timedelta(seconds=20)]


async def test_stop_interrupts_the_settling_after_a_sleep() -> None:
    stop = asyncio.Event()
    now = [CLOSE - timedelta(hours=1)]
    runs: list[int] = []

    async def action() -> None:
        runs.append(1)

    with capture_logs() as logs:
        task = asyncio.create_task(
            run_on_candle_close(
                "4h",
                stop,
                action,
                clock=lambda: now[0],
                poll=timedelta(milliseconds=5),
                settle=timedelta(hours=1),
            )
        )
        await asyncio.sleep(0.02)
        now[0] = CLOSE + timedelta(hours=1)
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert runs == []
    assert "schedule.late" in [entry["event"] for entry in logs]
