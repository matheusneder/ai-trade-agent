"""Candles (klines) as a ``pandas.DataFrame`` for technical analysis.

The analysis uses ``float64`` (required by TA-Lib); order prices stay in ``Decimal``.
"""

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
    raw = await api.klines(symbol, interval, limit=min(limit + 1, 1000))
    return closed_only(to_frame(raw), now_ms).tail(limit)
