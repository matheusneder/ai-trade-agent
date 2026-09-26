"""Indicadores técnicos (TA-Lib) calculados sobre candles fechados."""

from dataclasses import dataclass

import numpy as np
import pandas as pd
import talib

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class FeatureParams:
    ema_fast: int = 50
    ema_slow: int = 200
    adx_period: int = 14
    rsi_period: int = 14
    atr_period: int = 14
    roc_period: int = 10
    bb_period: int = 20
    volume_period: int = 20
    obv_period: int = 10
    breakout_lookback: int = 20
    rs_period: int = 20

    @property
    def warmup(self) -> int:
        """Candles necessários até todos os indicadores estarem definidos."""
        longest = max(
            self.ema_slow,
            self.adx_period * 2,
            self.bb_period,
            self.volume_period,
            self.breakout_lookback + 1,
            self.rs_period + 1,
        )
        return longest + 1


def _array(series: pd.Series) -> np.ndarray:
    return series.to_numpy(dtype="float64")


def compute_features(
    frame: pd.DataFrame,
    params: FeatureParams | None = None,
    benchmark_close: pd.Series | None = None,
) -> pd.DataFrame:
    """Retorna uma cópia de ``frame`` com as colunas de indicadores.

    ``benchmark_close`` (ex.: BTCUSDT no mesmo intervalo) habilita a força relativa.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"colunas ausentes: {missing}")
    p = params or FeatureParams()
    out = frame.copy()
    open_, high, low = _array(frame["open"]), _array(frame["high"]), _array(frame["low"])
    close, volume = _array(frame["close"]), _array(frame["volume"])

    out["ema_fast"] = talib.EMA(close, timeperiod=p.ema_fast)
    out["ema_slow"] = talib.EMA(close, timeperiod=p.ema_slow)
    out["adx"] = talib.ADX(high, low, close, timeperiod=p.adx_period)
    out["rsi"] = talib.RSI(close, timeperiod=p.rsi_period)
    _, _, macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    out["macd_hist"] = macd_hist
    out["roc"] = talib.ROC(close, timeperiod=p.roc_period)
    atr = talib.ATR(high, low, close, timeperiod=p.atr_period)
    out["atr"] = atr
    out["atr_pct"] = atr / close
    upper, middle, lower = talib.BBANDS(close, timeperiod=p.bb_period, nbdevup=2, nbdevdn=2)
    out["bb_width"] = (upper - lower) / middle
    volume_mean = talib.SMA(volume, timeperiod=p.volume_period)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["vol_rel"] = np.where(volume_mean > 0, volume / volume_mean, np.nan)
    obv_slope = talib.LINEARREG_SLOPE(talib.OBV(close, volume), timeperiod=p.obv_period)
    out["obv_trend"] = np.sign(obv_slope)
    out["breakout_high"] = frame["high"].rolling(p.breakout_lookback).max().shift(1)
    out["bullish"] = close > open_

    if benchmark_close is not None:
        reference = _array(benchmark_close.reindex(frame.index).ffill())
        out["rs_roc"] = talib.ROC(close / reference, timeperiod=p.rs_period)
    else:
        reference = close
        out["rs_roc"] = np.nan
    # Regime de mercado: referência (benchmark ou o próprio ativo) acima da EMA lenta.
    with np.errstate(invalid="ignore"):
        out["regime_up"] = reference > talib.EMA(reference, timeperiod=p.ema_slow)
    return out
