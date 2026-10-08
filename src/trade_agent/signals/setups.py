"""Technical score and entry setups (vectorized, for live and backtest).

Score ∈ [-1, 1], a sum of simple, auditable components:

==========================  ===============================================
trend (±0.35)               fast EMA vs slow EMA and price vs slow EMA
momentum (±0.20)            MACD histogram
strength (±0.15)            ADX ≥ minimum, in the direction of the trend
relative strength (±0.20)   ROC of the price/benchmark ratio (e.g. BTC)
volume (±0.10)              volume above average on an up/down candle
stretched (−0.15)           RSI above the overbought level
==========================  ===============================================

Setups (buy only, Spot market), both with the trend and only while the market regime is
up (benchmark above the slow EMA; can be turned off with ``use_regime_filter``):

* ``trend_pullback``: uptrend with ADX ≥ minimum, RSI coming out of a pullback
  (previous ≤ limit and rising) on an up candle;
* ``breakout``: close above the high of the previous N candles, with relative volume
  ≥ minimum.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

TREND_PULLBACK = "trend_pullback"
BREAKOUT = "breakout"


@dataclass(frozen=True)
class SignalParams:
    adx_min: float = 20.0
    rsi_pullback_max: float = 45.0
    rsi_overbought: float = 75.0
    vol_rel_min: float = 1.5
    atr_stop_mult: float = 2.0
    min_stop_pct: float = 0.01
    max_stop_pct: float = 0.08
    use_regime_filter: bool = True


@dataclass(frozen=True)
class Signal:
    """Reading of the last closed candle."""

    score: float
    setup: str | None
    stop_pct: float
    """Suggested stop distance (fraction of the price), from the ATR."""
    atr_pct: float
    close: float

    def is_entry(self, min_score: float) -> bool:
        return self.setup is not None and self.score >= min_score


def _col(features: pd.DataFrame, name: str) -> pd.Series:
    return features[name].astype("float64")


def _uptrend(f: pd.DataFrame) -> pd.Series:
    return (_col(f, "ema_fast") > _col(f, "ema_slow")) & (_col(f, "close") > _col(f, "ema_slow"))


def _downtrend(f: pd.DataFrame) -> pd.Series:
    return (_col(f, "ema_fast") < _col(f, "ema_slow")) & (_col(f, "close") < _col(f, "ema_slow"))


def score_frame(features: pd.DataFrame, params: SignalParams | None = None) -> pd.Series:
    p = params or SignalParams()
    up, down = _uptrend(features), _downtrend(features)
    direction = up.astype(float) - down.astype(float)
    macd = _col(features, "macd_hist")
    rs = _col(features, "rs_roc")
    vol_rel = _col(features, "vol_rel")
    bullish = features["bullish"].astype(bool)
    heavy = vol_rel > 1

    score = 0.35 * direction
    score += 0.20 * np.sign(macd.fillna(0.0))
    score += 0.15 * direction * (_col(features, "adx") >= p.adx_min)
    score += 0.20 * np.sign(rs.fillna(0.0))
    score += 0.10 * (heavy & bullish) - 0.10 * (heavy & ~bullish)
    score -= 0.15 * (_col(features, "rsi") > p.rsi_overbought)
    result: pd.Series = score.clip(-1.0, 1.0).rename("score")
    return result


def setup_frame(features: pd.DataFrame, params: SignalParams | None = None) -> pd.Series:
    """Setup name on each candle (``""`` when there is no setup)."""
    p = params or SignalParams()
    rsi = _col(features, "rsi")
    trend = _uptrend(features)
    if p.use_regime_filter:
        trend &= features["regime_up"].astype(bool)
    pullback = (
        trend
        & (_col(features, "adx") >= p.adx_min)
        & (rsi.shift(1) <= p.rsi_pullback_max)
        & (rsi > rsi.shift(1))
        & features["bullish"].astype(bool)
    )
    breakout = (
        trend
        & (_col(features, "close") > _col(features, "breakout_high"))
        & (_col(features, "vol_rel") >= p.vol_rel_min)
    )
    labels = np.where(breakout, BREAKOUT, np.where(pullback, TREND_PULLBACK, ""))
    return pd.Series(labels, index=features.index, name="setup")


def stop_pct_frame(features: pd.DataFrame, params: SignalParams | None = None) -> pd.Series:
    p = params or SignalParams()
    stop = p.atr_stop_mult * _col(features, "atr_pct")
    return stop.clip(p.min_stop_pct, p.max_stop_pct).rename("stop_pct")


def entry_mask(
    features: pd.DataFrame, min_score: float, params: SignalParams | None = None
) -> pd.Series:
    """Candles with a setup and the minimum score (entry signal)."""
    has_setup = setup_frame(features, params) != ""
    return (has_setup & (score_frame(features, params) >= min_score)).rename("entry")


def exit_mask(
    features: pd.DataFrame, exit_score: float, params: SignalParams | None = None
) -> pd.Series:
    """Candles where the score fell to the exit level (rotation)."""
    return (score_frame(features, params) <= exit_score).rename("exit")


def evaluate(features: pd.DataFrame, params: SignalParams | None = None) -> Signal:
    """Signal of the last closed candle (NaN → neutral)."""
    if features.empty:
        raise ValueError("sem candles para avaliar")
    last = features.index[-1]
    score = float(score_frame(features, params).loc[last])
    setup = str(setup_frame(features, params).loc[last]) or None
    stop = float(stop_pct_frame(features, params).loc[last])
    atr_pct = float(features["atr_pct"].loc[last])
    return Signal(
        score=score,
        setup=setup,
        stop_pct=(params or SignalParams()).max_stop_pct if np.isnan(stop) else stop,
        atr_pct=0.0 if np.isnan(atr_pct) else atr_pct,
        close=float(features["close"].loc[last]),
    )
