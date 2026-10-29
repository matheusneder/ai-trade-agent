"""Pair-local strategy families (D-044): entries and exits read only the pair's own candles.

The legacy family (:mod:`trade_agent.signals.setups`) vetoes every setup while the benchmark
(BTC) closes below its EMA 200 and scores the relative strength against it. The families
here read neither ``regime_up`` nor ``rs_roc``: the benchmark's trajectory, its side of the
EMA 200 or its absence cannot change their score, setup, stop, target or technical exit.
They only buy (Spot). The default parameters are research seeds (doc/lab-results.md, section
2.10), not a calibration.

``pair_pullback_rsi``
    Local uptrend (EMA 20 > EMA 50, the EMA 50 rising over 5 candles, ADX ≥ ``adx_min``). The
    RSI was ≤ ``rsi_dip`` on one of the 3 previous candles and crosses ``rsi_cross`` upwards,
    staying ≤ ``rsi_max``, on an up candle that closes above the EMA 20. Filters: the close at
    most ``max_extension_atr`` ATRs above the EMA 20, and a relative volume ≥ ``vol_rel_min``
    (or, with ``obv_confirm``, a rising OBV instead). Stop ``stop_buffer_atr`` ATRs below the
    lowest low of the 5 previous candles and the signal candle. Technical exit after two
    closes below the EMA 20 with the RSI below ``exit_rsi``.

``pair_volatility_breakout``
    The Bollinger width (20, 2σ) below its ``squeeze_pctile`` quantile of the 120 candles
    before it, on one of the 3 previous candles; then a close above the high of the 20
    previous candles, with EMA 20 > EMA 50, ``rsi_min`` ≤ RSI ≤ ``rsi_max`` and a relative
    volume ≥ ``vol_rel_min``. Filters: the close at most ``max_extension_atr`` ATRs above the
    broken level, a true range of at most ``max_range_atr`` times the ATR before the candle
    and, with ``adx_filter``, ADX ≥ ``adx_min`` or rising. With ``retest`` the entry waits up
    to 3 candles for an up candle that dips to the level (``retest_tolerance_atr``) and closes
    above it. Stop ``stop_buffer_atr`` ATRs below the level. Technical exit on a close back
    below the level, frozen at the entry (:func:`position_exit`).

``pair_range_reversion``
    A local range: ADX ≤ ``adx_max``, the EMA 50 moving less than ``max_slope_atr`` ATRs over 5
    candles, the Bollinger width at most its ``max_width_pctile`` quantile of the 120 candles
    before it, and the EMA 20 at most ``max_trend_gap_atr`` ATRs below the EMA 50 (no
    persistent local decline). The previous candle closed below the lower band; this one is
    an up candle that closes back inside, with the RSI crossing ``rsi_cross`` upwards. Stop
    ``stop_buffer_atr`` ATRs below the low of both candles; the target is the middle band,
    frozen on the signal (``Signal.target_pct``). Technical exit on a close below the lower
    band.

Every stop is at least ``min_stop_atr`` ATRs away; a setup whose stop would sit beyond
``max_stop_pct`` is discarded, never tightened to fit (``filtered`` says why). The ranking
score is the legacy one without the relative strength (``SignalParams.local_score``).
"""

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from trade_agent.signals.setups import RATIO_TOLERANCE, Signal, SignalParams, score_frame


class Family(StrEnum):
    LEGACY = "legacy"
    PAIR_PULLBACK_RSI = "pair_pullback_rsi"
    PAIR_VOLATILITY_BREAKOUT = "pair_volatility_breakout"
    PAIR_RANGE_REVERSION = "pair_range_reversion"

    @property
    def local(self) -> bool:
        """Decides on the pair's own candles only."""
        return self is not Family.LEGACY


PAIR_PULLBACK = "pair_pullback"
PAIR_BREAKOUT = "pair_breakout"
PAIR_RANGE = "pair_range"

DIP_CANDLES = 3
SLOPE_CANDLES = 5
STOP_CANDLES = 5
SQUEEZE_WINDOW = 120
SQUEEZE_CANDLES = 3
RETEST_CANDLES = 3

LOCAL_SCORE = SignalParams(local_score=True)
"""Ranking score of the local families: the legacy components without the benchmark."""

# Filter reasons (operator interface: Portuguese).
EXTENDED = "extensão acima do limite"
NO_VOLUME = "volume sem confirmação"
WIDE_STOP = "stop acima do teto"
WIDE_RANGE = "candle amplo demais"
NO_ADX = "ADX sem força"
NO_TARGET = "alvo abaixo do preço"


@dataclass(frozen=True)
class PullbackParams:
    adx_min: float = 20.0
    """0 turns the ADX requirement off (ablation)."""
    rsi_dip: float = 40.0
    rsi_cross: float = 45.0
    rsi_max: float = 65.0
    max_extension_atr: float = 1.0
    """0: off."""
    max_drift_atr: float = 0.5
    """No entry once the executable price is this many ATRs above the signal's close; 0: off."""
    vol_rel_min: float = 1.0
    """0: off."""
    obv_confirm: bool = False
    """A rising OBV instead of the relative volume (needs ``vol_rel_min`` 0)."""
    stop_buffer_atr: float = 0.25
    exit_rsi: float = 45.0
    min_stop_atr: float = 1.5
    max_stop_pct: float = 0.06

    def __post_init__(self) -> None:
        if self.obv_confirm and self.vol_rel_min > 0:
            raise ValueError("obv_confirm substitui vol_rel_min: use vol_rel_min 0")


@dataclass(frozen=True)
class BreakoutParams:
    squeeze_pctile: float = 0.20
    rsi_min: float = 50.0
    rsi_max: float = 70.0
    vol_rel_min: float = 1.8
    max_extension_atr: float = 1.0
    """0: off."""
    max_range_atr: float = 2.5
    """0: off."""
    adx_filter: bool = False
    adx_min: float = 20.0
    """Only with ``adx_filter``."""
    retest: bool = False
    retest_tolerance_atr: float = 0.25
    """Only with ``retest``."""
    stop_buffer_atr: float = 0.5
    min_stop_atr: float = 2.0
    max_stop_pct: float = 0.07


@dataclass(frozen=True)
class RangeParams:
    adx_max: float = 20.0
    max_slope_atr: float = 0.25
    max_width_pctile: float = 0.80
    max_trend_gap_atr: float = 1.0
    rsi_cross: float = 30.0
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 1.5
    max_stop_pct: float = 0.04


type LocalParams = PullbackParams | BreakoutParams | RangeParams

PARAMS: dict[Family, type[LocalParams]] = {
    Family.PAIR_PULLBACK_RSI: PullbackParams,
    Family.PAIR_VOLATILITY_BREAKOUT: BreakoutParams,
    Family.PAIR_RANGE_REVERSION: RangeParams,
}
STOP_PARAMS = frozenset({"min_stop_atr", "max_stop_pct"})
"""Set from ``protection.stop`` (``atr_mult``, ``max_pct``), never in ``signals``."""


def family_of(params: SignalParams | LocalParams) -> Family:
    for family, kind in PARAMS.items():
        if isinstance(params, kind):
            return family
    return Family.LEGACY


def _col(f: pd.DataFrame, name: str) -> pd.Series:
    return f[name].astype("float64")


def _stop(f: pd.DataFrame, stop_price: pd.Series, p: LocalParams) -> tuple[pd.Series, pd.Series]:
    """Stop distance (fraction of the close), at least ``min_stop_atr`` ATRs, and whether it
    fits under ``max_stop_pct``."""
    close = _col(f, "close")
    floor = p.min_stop_atr * _col(f, "atr_pct")
    stop = pd.Series(np.maximum((close - stop_price) / close, floor), index=f.index)
    return stop, stop <= p.max_stop_pct * (1 + RATIO_TOLERANCE)


def _slope(f: pd.DataFrame) -> pd.Series:
    fast = _col(f, "ema_fast")
    return fast - fast.shift(SLOPE_CANDLES)


def _bullish(f: pd.DataFrame) -> pd.Series:
    return f["bullish"].astype(bool)


def _quantile_before(width: pd.Series, q: float) -> pd.Series:
    """The ``q`` quantile of the ``SQUEEZE_WINDOW`` candles before each one (causal)."""
    return width.shift(1).rolling(SQUEEZE_WINDOW).quantile(q)


def _reasons(pattern: pd.Series, checks: list[tuple[pd.Series, str]]) -> pd.Series:
    """First failed check of each candle with the pattern (``""`` otherwise)."""
    reason = pd.Series("", index=pattern.index, dtype=object)
    for ok, label in reversed(checks):
        reason = reason.mask(pattern & ~ok, label)
    return reason


def _pullback(f: pd.DataFrame, p: PullbackParams) -> pd.DataFrame:
    close, low, rsi, atr = _col(f, "close"), _col(f, "low"), _col(f, "rsi"), _col(f, "atr")
    short, fast = _col(f, "ema_short"), _col(f, "ema_fast")
    trend = (short > fast) & (_slope(f) > 0) & (_col(f, "adx") >= p.adx_min)
    dipped = rsi.shift(1).rolling(DIP_CANDLES).min() <= p.rsi_dip
    crossed = (rsi.shift(1) < p.rsi_cross) & (rsi >= p.rsi_cross) & (rsi <= p.rsi_max)
    pattern = trend & dipped & crossed & _bullish(f) & (close > short)
    extension_ok = (close - short <= p.max_extension_atr * atr) | (p.max_extension_atr <= 0)
    if p.obv_confirm:
        volume_ok = _col(f, "obv_trend") > 0
    else:
        volume_ok = (_col(f, "vol_rel") >= p.vol_rel_min) | (p.vol_rel_min <= 0)
    stop_price = np.minimum(low.shift(1).rolling(STOP_CANDLES).min(), low) - p.stop_buffer_atr * atr
    stop, fits = _stop(f, stop_price, p)
    checks = [(extension_ok, EXTENDED), (volume_ok, NO_VOLUME), (fits, WIDE_STOP)]
    setup = pattern & extension_ok & volume_ok & fits
    cap = close + p.max_drift_atr * atr if p.max_drift_atr > 0 else np.nan
    return pd.DataFrame(
        {
            "setup": np.where(setup, PAIR_PULLBACK, ""),
            "stop_pct": stop,
            "target_pct": np.nan,
            "entry_cap": pd.Series(cap, index=f.index).where(setup),
            "level": np.nan,
            "exit": (close < short) & (close.shift(1) < short.shift(1)) & (rsi < p.exit_rsi),
            "filtered": _reasons(pattern, checks),
        },
        index=f.index,
    )


def _breakout(f: pd.DataFrame, p: BreakoutParams) -> pd.DataFrame:
    close, low, rsi, atr = _col(f, "close"), _col(f, "low"), _col(f, "rsi"), _col(f, "atr")
    width, level = _col(f, "bb_width"), _col(f, "breakout_high")
    squeezed = (width < _quantile_before(width, p.squeeze_pctile)).astype(float)
    recent_squeeze = squeezed.shift(1).rolling(SQUEEZE_CANDLES).max() > 0
    trend = _col(f, "ema_short") > _col(f, "ema_fast")
    rsi_ok = (rsi >= p.rsi_min) & (rsi <= p.rsi_max)
    pattern = recent_squeeze & (close > level) & trend & rsi_ok
    volume_ok = _col(f, "vol_rel") >= p.vol_rel_min
    extension_ok = (close - level <= p.max_extension_atr * atr) | (p.max_extension_atr <= 0)
    range_ok = (_col(f, "true_range") <= p.max_range_atr * atr.shift(1)) | (p.max_range_atr <= 0)
    adx = _col(f, "adx")
    adx_ok = (adx >= p.adx_min) | (adx > adx.shift(1)) | (not p.adx_filter)
    checks = [(volume_ok, NO_VOLUME), (extension_ok, EXTENDED), (range_ok, WIDE_RANGE)]
    checks.append((adx_ok, NO_ADX))
    broke = pattern & volume_ok & extension_ok & range_ok & adx_ok
    entry_level = level.where(broke)
    reasons = _reasons(pattern, checks)
    if p.retest:  # the breakout candle only arms the setup; the entry is the retest
        entry_level = pd.Series(np.nan, index=f.index)
        for k in range(RETEST_CANDLES, 0, -1):  # the earliest retest wins
            armed = level.shift(k).where(broke.shift(k, fill_value=False).astype(bool))
            held = (low <= armed + p.retest_tolerance_atr * atr) & (close > armed) & _bullish(f)
            entry_level = armed.where(held, entry_level)
        pattern = entry_level.notna()
        reasons = pd.Series("", index=f.index, dtype=object)
    stop, fits = _stop(f, entry_level - p.stop_buffer_atr * atr, p)
    setup = pattern & entry_level.notna() & fits
    return pd.DataFrame(
        {
            "setup": np.where(setup, PAIR_BREAKOUT, ""),
            "stop_pct": stop,
            "target_pct": np.nan,
            "entry_cap": np.nan,
            "level": entry_level.where(setup),
            "exit": False,
            "filtered": reasons.mask(pattern & entry_level.notna() & ~fits, WIDE_STOP),
        },
        index=f.index,
    )


def _range(f: pd.DataFrame, p: RangeParams) -> pd.DataFrame:
    close, low, rsi, atr = _col(f, "close"), _col(f, "low"), _col(f, "rsi"), _col(f, "atr")
    lower, width = _col(f, "bb_lower"), _col(f, "bb_width")
    gap = _col(f, "ema_fast") - _col(f, "ema_short")
    ranging = (
        (_col(f, "adx") <= p.adx_max)
        & (_slope(f).abs() < p.max_slope_atr * atr)
        & (width <= _quantile_before(width, p.max_width_pctile))
        & (gap <= p.max_trend_gap_atr * atr)
    )
    back_inside = (close.shift(1) < lower.shift(1)) & (close > lower) & _bullish(f)
    crossed = (rsi.shift(1) < p.rsi_cross) & (rsi >= p.rsi_cross)
    pattern = ranging & back_inside & crossed
    stop, fits = _stop(f, np.minimum(low, low.shift(1)) - p.stop_buffer_atr * atr, p)
    target = _col(f, "bb_middle") / close - 1
    target_ok = target > 0
    setup = pattern & target_ok & fits
    return pd.DataFrame(
        {
            "setup": np.where(setup, PAIR_RANGE, ""),
            "stop_pct": stop,
            "target_pct": target.where(setup),
            "entry_cap": np.nan,
            "level": np.nan,
            "exit": close < lower,
            "filtered": _reasons(pattern, [(target_ok, NO_TARGET), (fits, WIDE_STOP)]),
        },
        index=f.index,
    )


def local_regime_frame(features: pd.DataFrame) -> pd.Series:
    """The pair's own trend on each candle: ``alta``, ``baixa`` or ``lateral``."""
    short, fast, slope = _col(features, "ema_short"), _col(features, "ema_fast"), _slope(features)
    up, down = (short > fast) & (slope > 0), (short < fast) & (slope < 0)
    labels = np.where(up, "alta", np.where(down, "baixa", "lateral"))
    return pd.Series(labels, index=features.index, name="local_regime")


def local_frame(features: pd.DataFrame, params: LocalParams) -> pd.DataFrame:
    """Setup, stop, target, entry cap, level, technical exit and filter reason on each candle,
    plus the ranking score and the local regime; ``features`` must carry the local columns
    (:func:`~trade_agent.signals.features.compute_local_features`)."""
    if isinstance(params, PullbackParams):
        frame = _pullback(features, params)
    elif isinstance(params, BreakoutParams):
        frame = _breakout(features, params)
    else:
        frame = _range(features, params)
    frame["exit"] = frame["exit"].astype(bool)
    frame["score"] = score_frame(features, LOCAL_SCORE)
    frame["local_regime"] = local_regime_frame(features)
    return frame


def _optional(value: float) -> float | None:
    return None if np.isnan(value) else float(value)


def evaluate_local(features: pd.DataFrame, params: LocalParams) -> Signal:
    """Signal of the last closed candle (NaN → neutral), like :func:`evaluate`."""
    if features.empty:
        raise ValueError("sem candles para avaliar")
    last = local_frame(features, params).iloc[-1]
    stop, atr_pct = float(last["stop_pct"]), float(features["atr_pct"].iloc[-1])
    return Signal(
        score=float(last["score"]),
        setup=str(last["setup"]) or None,
        stop_pct=params.max_stop_pct if np.isnan(stop) else stop,
        atr_pct=0.0 if np.isnan(atr_pct) else atr_pct,
        close=float(features["close"].iloc[-1]),
        target_pct=_optional(float(last["target_pct"])),
        max_entry_price=_optional(float(last["entry_cap"])),
        level=_optional(float(last["level"])),
        exit_signal=bool(last["exit"]),
        filtered=str(last["filtered"]) or None,
        local_regime=str(last["local_regime"]),
    )


TECHNICAL_EXITS = {
    Family.PAIR_PULLBACK_RSI: "saída técnica: dois fechamentos abaixo da EMA20 com RSI baixo",
    Family.PAIR_VOLATILITY_BREAKOUT: "saída técnica: fechou abaixo do nível rompido",
    Family.PAIR_RANGE_REVERSION: "saída técnica: fechou abaixo da banda inferior",
}


def position_exit(signal: Signal, params: LocalParams, entry_level: float | None) -> str | None:
    """Technical exit of an open position on the last closed candle (``signal``), or ``None``.

    The breakout family exits on a close below the level its entry broke (``entry_level``,
    from the entry's signal); the others on their candle rule (``Signal.exit_signal``)."""
    family = family_of(params)
    if isinstance(params, BreakoutParams):
        failed = entry_level is not None and signal.close < entry_level
        return TECHNICAL_EXITS[family] if failed else None
    return TECHNICAL_EXITS[family] if signal.exit_signal else None
