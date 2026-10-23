"""In-memory market tape for the Demo exit audit (``trade_agent.demo_audit``)."""

from collections.abc import Iterable
from decimal import Decimal

from trade_agent.demo_audit import MINUTE_MS, Candle
from trade_agent.exchange.models import AggTrade

BASE_MS = 1_791_590_400_000
"""2026-10-10 00:00 UTC."""


def at(minute: int, second: float = 0) -> int:
    """Timestamp (ms) ``minute`` minutes and ``second`` seconds after ``BASE_MS``."""
    return BASE_MS + minute * MINUTE_MS + round(second * 1000)


class FakeTape:
    """Trades given as ``(time_ms, price, buyer_is_maker)``; 1m candles derived from them."""

    def __init__(self, trades: Iterable[tuple[int, str, bool]]) -> None:
        self._trades = [
            AggTrade.model_validate({"a": i, "p": price, "q": "1", "T": time, "m": maker})
            for i, (time, price, maker) in enumerate(sorted(trades))
        ]
        self.trade_calls: list[tuple[int, int]] = []

    async def candles(self, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
        first = start_ms - start_ms % MINUTE_MS
        minutes: dict[int, list[Decimal]] = {}
        for trade in self._trades:
            minute = trade.time - trade.time % MINUTE_MS
            if first <= minute <= end_ms:
                minutes.setdefault(minute, []).append(trade.price)
        return [Candle(m, max(prices), min(prices)) for m, prices in sorted(minutes.items())]

    async def trades(self, symbol: str, start_ms: int, end_ms: int) -> list[AggTrade]:
        self.trade_calls.append((start_ms, end_ms))
        return [t for t in self._trades if start_ms <= t.time <= end_ms]
