"""Candles (klines) em ``pandas.DataFrame`` para análise técnica.

A análise usa ``float64`` (exigido pela TA-Lib); preços de ordens continuam em ``Decimal``.
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
    """Converte a resposta de ``/api/v3/klines`` em DataFrame indexado pela abertura (UTC)."""
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
    """Remove o candle ainda aberto (``close_time`` no futuro)."""
    return frame[frame["close_time"] < now_ms]


async def fetch_candles(
    api: BinanceSpotApi, symbol: str, interval: str, *, limit: int, now_ms: int
) -> pd.DataFrame:
    """Busca os últimos candles **fechados** do símbolo."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"intervalo não suportado: {interval}")
    raw = await api.klines(symbol, interval, limit=min(limit + 1, 1000))
    return closed_only(to_frame(raw), now_ms).tail(limit)
