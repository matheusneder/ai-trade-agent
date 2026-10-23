"""Candles (klines) as a ``pandas.DataFrame`` for technical analysis.

The analysis uses ``float64`` (required by TA-Lib); order prices stay in ``Decimal``.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

import pandas as pd

from trade_agent.exchange.api import BinanceSpotApi

COLUMNS = ("open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume")
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
MAX_KLINES = 1000
"""Largest ``limit`` of one ``/api/v3/klines`` request (weight 2 for any limit)."""


def to_frame(raw: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """Converts the ``/api/v3/klines`` response into a DataFrame indexed by open time (UTC)."""
    rows = [
        (
            int(k[0]),
            float(k[1]),
            float(k[2]),
            float(k[3]),
            float(k[4]),
            float(k[5]),
            int(k[6]),
            float(k[7]),
        )
        for k in raw
    ]
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    frame.index = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame.index.name = "time"
    return frame


def closed_only(frame: pd.DataFrame, now_ms: int) -> pd.DataFrame:
    """Drops the candle that is still open (``close_time`` in the future)."""
    return frame[frame["close_time"] < now_ms]


async def fetch_candles(
    api: BinanceSpotApi, symbol: str, interval: str, *, limit: int, now_ms: int
) -> pd.DataFrame:
    """Fetches the symbol's latest **closed** candles."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"intervalo não suportado: {interval}")
    raw = await api.klines(symbol, interval, limit=min(limit + 1, MAX_KLINES))
    return closed_only(to_frame(raw), now_ms).tail(limit)


class ClosedCandleCache:
    """Share a closed-candle snapshot between profiles, with bounded memory and no Redis.

    Requests are serialized to avoid duplicate downloads. A returned frame is a copy;
    indicator calculations cannot mutate another profile's snapshot. Failed/empty reads
    are never cached, and advancing a candle invalidates that symbol's previous snapshot.
    """

    def __init__(self, api: BinanceSpotApi, *, max_entries: int = 256) -> None:
        self._api = api
        self._max_entries = max_entries
        self._lock = asyncio.Lock()
        self._frames: dict[tuple[str, str, int], tuple[int, pd.DataFrame]] = {}

    async def fetch(self, symbol: str, interval: str, *, limit: int, now_ms: int) -> pd.DataFrame:
        period = now_ms // INTERVAL_MS[interval]
        key = (symbol, interval, limit)
        async with self._lock:
            cached = self._frames.get(key)
            if cached is not None and cached[0] == period:
                return cached[1].copy()
            frame = await fetch_candles(self._api, symbol, interval, limit=limit, now_ms=now_ms)
            # Do not pin a stale response for an entire candle if exchange data is delayed.
            expected_open = (period - 1) * INTERVAL_MS[interval]
            if not frame.empty and int(frame["open_time"].iloc[-1]) == expected_open:
                self._frames.pop(key, None)
                if len(self._frames) >= self._max_entries:
                    self._frames.pop(next(iter(self._frames)))
                self._frames[key] = (period, frame)
            return frame.copy()
