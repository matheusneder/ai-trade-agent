"""Séries OHLCV sintéticas e determinísticas para testar sinais e backtests."""

import math
from typing import Any

import numpy as np
import pandas as pd

HOUR_MS = 3_600_000
START_MS = 1_780_000_000_000 - (1_780_000_000_000 % HOUR_MS)


def ohlcv(
    closes: list[float] | np.ndarray,
    *,
    volumes: list[float] | np.ndarray | None = None,
    interval_ms: int = HOUR_MS,
    start_ms: int = START_MS,
) -> pd.DataFrame:
    """DataFrame OHLCV no formato de ``market.candles.to_frame`` a partir dos fechamentos."""
    close = np.asarray(closes, dtype="float64")
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.002
    low = np.minimum(open_, close) * 0.998
    volume = np.asarray(
        volumes if volumes is not None else np.full(len(close), 100.0), dtype="float64"
    )
    times = start_ms + np.arange(len(close)) * interval_ms
    frame = pd.DataFrame(
        {
            "open_time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "close_time": times + interval_ms - 1,
            "quote_volume": close * volume,
        }
    )
    frame.index = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame.index.name = "time"
    return frame


def raw_klines(frame: pd.DataFrame) -> list[list[Any]]:
    """Converte o DataFrame sintético no formato bruto de ``/api/v3/klines``."""
    columns = [frame[c].tolist() for c in ("open_time", "open", "high", "low", "close", "volume")]
    tail = [frame[c].tolist() for c in ("close_time", "quote_volume")]
    return [
        [int(ot), str(o), str(h), str(lo), str(c), str(v), int(ct), str(qv), 10, "0", "0", "0"]
        for (ot, o, h, lo, c, v), (ct, qv) in zip(
            zip(*columns, strict=True), zip(*tail, strict=True), strict=True
        )
    ]


def uptrend_with_pullback(n: int = 320) -> pd.DataFrame:
    """Alta firme, recuo curto no fim e retomada no último candle (setup de pullback)."""
    base = [100 * math.exp(0.004 * i) for i in range(n - 9)]
    last = base[-1]
    pullback = [last * (1 - 0.008 * k) for k in range(1, 9)]
    resume = [pullback[-1] * 1.01]
    return ohlcv(base + pullback + resume)


def breakout(n: int = 320) -> pd.DataFrame:
    """Alta moderada, lateralização e rompimento com volume no último candle."""
    trend = [100 * math.exp(0.002 * i) for i in range(n - 40)]
    flat = [trend[-1] * (1 + 0.003 * math.sin(i)) for i in range(39)]
    closes = trend + flat + [trend[-1] * 1.03]
    volumes = [100.0] * (n - 1) + [400.0]
    return ohlcv(closes, volumes=volumes)


def downtrend(n: int = 320) -> pd.DataFrame:
    return ohlcv([100 * math.exp(-0.004 * i) for i in range(n)])
