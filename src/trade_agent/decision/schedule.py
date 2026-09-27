"""Agendamento no fechamento do candle (mesma cadência validada no laboratório)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from trade_agent.market.candles import INTERVAL_MS


def next_candle_close(timeframe: str, now: datetime) -> datetime:
    """Próximo fechamento de candle do ``timeframe`` (UTC), estritamente após ``now``."""
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
    """Executa ``action`` logo após cada fechamento (``delay`` para o candle consolidar)."""
    while not stop.is_set():
        now = clock()
        # a partir de (now - delay): acordar entre o fechamento e o atraso não pula o ciclo
        target = next_candle_close(timeframe, now - delay) + delay
        wait_s = (target - now).total_seconds()
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(wait_s, 0))
        except TimeoutError:
            await action()
