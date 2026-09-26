import math

import numpy as np
import pandas as pd
import pytest

from tests.support.candles import breakout, downtrend, ohlcv, uptrend_with_pullback
from trade_agent.signals import (
    FeatureParams,
    SignalParams,
    compute_features,
    entry_mask,
    evaluate,
    exit_mask,
    score_frame,
    setup_frame,
    stop_pct_frame,
)
from trade_agent.signals.setups import BREAKOUT, TREND_PULLBACK

FEATURES = (
    "ema_fast", "ema_slow", "adx", "rsi", "macd_hist", "roc", "atr", "atr_pct",
    "bb_width", "vol_rel", "obv_trend", "breakout_high", "bullish", "rs_roc", "regime_up",
)  # fmt: skip


def test_compute_features_adds_all_columns_without_mutating_input() -> None:
    frame = uptrend_with_pullback()
    before = frame.copy()
    features = compute_features(frame)
    for column in FEATURES:
        assert column in features.columns
    pd.testing.assert_frame_equal(frame, before)
    assert features["rs_roc"].isna().all()  # sem benchmark
    last = features.iloc[-1]
    assert last["ema_fast"] > last["ema_slow"]
    assert 0 < last["atr_pct"] < 0.1


def test_compute_features_requires_ohlcv_columns() -> None:
    with pytest.raises(ValueError, match="colunas ausentes"):
        compute_features(pd.DataFrame({"close": [1.0, 2.0]}))


def test_relative_strength_against_benchmark() -> None:
    asset = uptrend_with_pullback()
    flat_benchmark = pd.Series(100.0, index=asset.index)
    features = compute_features(asset, benchmark_close=flat_benchmark)
    assert features["rs_roc"].iloc[-30:].notna().all()
    strong = compute_features(
        ohlcv([100 * math.exp(0.004 * i) for i in range(300)]),
        benchmark_close=pd.Series(100.0, index=asset.index[:300]),
    )
    assert strong["rs_roc"].iloc[-1] > 0


def test_zero_volume_gives_nan_relative_volume() -> None:
    frame = ohlcv(np.linspace(100, 110, 60), volumes=np.zeros(60))
    assert compute_features(frame)["vol_rel"].isna().all()


def test_warmup_covers_longest_indicator() -> None:
    params = FeatureParams(ema_slow=200)
    assert params.warmup == 201
    assert FeatureParams(ema_slow=10, adx_period=14).warmup == 29


def test_pullback_setup_detected() -> None:
    signal = evaluate(compute_features(uptrend_with_pullback()))
    assert signal.setup == TREND_PULLBACK
    assert signal.score > 0
    assert 0.01 <= signal.stop_pct <= 0.08
    assert signal.is_entry(min_score=0.2)
    assert not signal.is_entry(min_score=0.9)


def test_breakout_setup_detected_with_volume() -> None:
    features = compute_features(breakout())
    signal = evaluate(features)
    assert signal.setup == BREAKOUT
    assert signal.score >= 0.5
    quiet = compute_features(breakout().assign(volume=100.0))
    assert evaluate(quiet).setup != BREAKOUT


def test_regime_without_benchmark_uses_the_asset_itself() -> None:
    assert compute_features(uptrend_with_pullback())["regime_up"].iloc[-1]
    assert not compute_features(downtrend())["regime_up"].iloc[-1]
    assert not compute_features(ohlcv(np.linspace(100, 101, 5)))["regime_up"].any()


def test_regime_filter_blocks_setups_when_benchmark_is_falling() -> None:
    asset = uptrend_with_pullback()
    falling = downtrend(len(asset))["close"].set_axis(asset.index)
    features = compute_features(asset, benchmark_close=falling)
    assert not features["regime_up"].iloc[-1]
    assert evaluate(features).setup is None
    assert evaluate(features, SignalParams(use_regime_filter=False)).setup == TREND_PULLBACK
    rising = compute_features(asset, benchmark_close=asset["close"] * 2)
    assert evaluate(rising).setup == TREND_PULLBACK


def test_breakout_requires_uptrend() -> None:
    closes = [100 * math.exp(-0.003 * i) for i in range(300)]
    closes.append(closes[-1] * 1.5)  # rompimento forte, mas contra a tendência
    frame = ohlcv(closes, volumes=[100.0] * 300 + [400.0])
    last = compute_features(frame).iloc[-1]
    assert last["close"] > last["breakout_high"]
    assert last["close"] > last["ema_slow"]
    assert last["vol_rel"] >= SignalParams().vol_rel_min
    assert last["ema_fast"] < last["ema_slow"]
    assert evaluate(compute_features(frame)).setup is None


def test_downtrend_has_negative_score_and_no_setup() -> None:
    features = compute_features(downtrend())
    signal = evaluate(features)
    assert signal.setup is None
    assert signal.score < 0
    assert exit_mask(features, exit_score=-0.2).iloc[-1]
    assert not entry_mask(features, min_score=0.0).iloc[-1]


def test_vectorized_frames_align_with_evaluate() -> None:
    features = compute_features(uptrend_with_pullback())
    params = SignalParams()
    assert score_frame(features, params).iloc[-1] == pytest.approx(evaluate(features, params).score)
    assert setup_frame(features).iloc[-1] == TREND_PULLBACK
    assert entry_mask(features, min_score=0.2).iloc[-1]
    stops = stop_pct_frame(features, SignalParams(atr_stop_mult=100.0))
    assert stops.dropna().eq(0.08).all()  # limitado por max_stop_pct


def test_score_penalizes_overbought_and_rewards_relative_strength() -> None:
    features = compute_features(uptrend_with_pullback())
    lenient = score_frame(features, SignalParams(rsi_overbought=101)).iloc[-100]
    strict = score_frame(features, SignalParams(rsi_overbought=10)).iloc[-100]
    assert lenient - strict == pytest.approx(0.15)
    boosted = features.assign(rs_roc=1.0)
    assert score_frame(boosted).iloc[-1] == pytest.approx(score_frame(features).iloc[-1] + 0.2)


def test_evaluate_with_insufficient_history_is_neutral() -> None:
    features = compute_features(ohlcv(np.linspace(100, 101, 5)))
    signal = evaluate(features)
    assert signal.setup is None
    assert signal.stop_pct == SignalParams().max_stop_pct
    assert signal.atr_pct == 0.0
    with pytest.raises(ValueError, match="sem candles"):
        evaluate(features.iloc[0:0])
