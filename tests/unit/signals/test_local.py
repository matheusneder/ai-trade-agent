"""Pair-local families (D-044): setups, filters, exits and independence from the benchmark."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from tests.support.candles import (
    breakout,
    ohlcv,
    pair_breakout,
    pair_pullback,
    pair_range,
    uptrend_with_pullback,
)
from trade_agent.signals import (
    BreakoutParams,
    Family,
    LocalFeatureParams,
    PullbackParams,
    RangeParams,
    SignalParams,
    compute_features,
    compute_local_features,
    evaluate_local,
    family_of,
    local_frame,
    position_exit,
    score_frame,
)
from trade_agent.signals.local import (
    EXTENDED,
    NO_ADX,
    NO_TARGET,
    NO_VOLUME,
    PAIR_BREAKOUT,
    PAIR_PULLBACK,
    PAIR_RANGE,
    TECHNICAL_EXITS,
    WIDE_RANGE,
    WIDE_STOP,
    LocalParams,
    local_regime_frame,
)

CASES: list[tuple[pd.DataFrame, LocalParams, str]] = [
    (pair_pullback(), PullbackParams(), PAIR_PULLBACK),
    (pair_breakout(), BreakoutParams(), PAIR_BREAKOUT),
    (pair_range(), RangeParams(), PAIR_RANGE),
]


def _local(frame: pd.DataFrame, benchmark: pd.Series | None = None) -> pd.DataFrame:
    return compute_local_features(compute_features(frame, benchmark_close=benchmark))


def _last(frame: pd.DataFrame, params: LocalParams) -> pd.Series:
    return local_frame(_local(frame), params).iloc[-1]


@pytest.mark.parametrize(("frame", "params", "setup"), CASES)
def test_each_family_fires_its_setup_on_its_pattern(
    frame: pd.DataFrame, params: LocalParams, setup: str
) -> None:
    signal = evaluate_local(_local(frame), params)
    assert signal.setup == setup
    assert signal.filtered is None
    assert params.min_stop_atr * signal.atr_pct <= signal.stop_pct <= params.max_stop_pct
    assert not signal.exit_signal


def test_the_local_columns_are_new_and_leave_the_legacy_ones_alone() -> None:
    features = compute_features(uptrend_with_pullback())
    local = compute_local_features(features, LocalFeatureParams())
    pd.testing.assert_frame_equal(local[features.columns], features)
    assert {"ema_short", "bb_upper", "bb_middle", "bb_lower", "true_range"} <= set(local.columns)
    assert local["bb_lower"].iloc[-1] < local["bb_middle"].iloc[-1] < local["bb_upper"].iloc[-1]
    assert LocalFeatureParams().warmup == 21


def test_the_pullback_needs_the_rsi_to_cross_back_not_to_be_low() -> None:
    frame = pair_pullback()
    signal = evaluate_local(_local(frame), PullbackParams())
    assert signal.max_entry_price == pytest.approx(signal.close * (1 + 0.5 * signal.atr_pct))
    # the candle before the setup: the RSI is low (≤ 40), still falling, no setup yet
    before = local_frame(_local(frame.iloc[:-1]), PullbackParams()).iloc[-1]
    assert before["setup"] == ""
    assert _local(frame.iloc[:-1])["rsi"].iloc[-1] <= 40
    # a higher crossing level the RSI does not reach: no setup
    assert _last(frame, PullbackParams(rsi_cross=60))["setup"] == ""


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        (PullbackParams(max_extension_atr=0.01), EXTENDED),
        (PullbackParams(vol_rel_min=5), NO_VOLUME),
        (PullbackParams(max_stop_pct=0.01), WIDE_STOP),
    ],
)
def test_pullback_filters_say_why_they_discarded_it(params: PullbackParams, reason: str) -> None:
    last = _last(pair_pullback(), params)
    assert (last["setup"], last["filtered"]) == ("", reason)


def test_pullback_ablations_turn_their_filters_off() -> None:
    frame = pair_pullback()
    for params in (
        PullbackParams(max_extension_atr=0, vol_rel_min=0, adx_min=0),
        PullbackParams(vol_rel_min=0, obv_confirm=True),
    ):
        assert _last(frame, params)["setup"] == PAIR_PULLBACK
    signal = evaluate_local(_local(frame), PullbackParams(max_drift_atr=0))
    assert signal.max_entry_price is None
    with pytest.raises(ValueError, match="obv_confirm"):
        PullbackParams(obv_confirm=True)


def test_pullback_exits_after_two_closes_below_the_short_ema_with_a_low_rsi() -> None:
    frame = pair_pullback()
    closes = [*frame["close"], *(frame["close"].iloc[-1] * 0.96**k for k in range(1, 4))]
    falling = ohlcv(closes)
    exits = local_frame(_local(falling), PullbackParams())["exit"]
    assert not exits.iloc[-4] and exits.iloc[-1]
    signal = evaluate_local(_local(falling), PullbackParams())
    expected = TECHNICAL_EXITS[Family.PAIR_PULLBACK_RSI]
    assert position_exit(signal, PullbackParams(), None) == expected
    assert (
        position_exit(evaluate_local(_local(frame), PullbackParams()), PullbackParams(), None)
        is None
    )


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        (BreakoutParams(vol_rel_min=5), NO_VOLUME),
        (BreakoutParams(max_extension_atr=0.01), EXTENDED),
        (BreakoutParams(max_range_atr=0.01), WIDE_RANGE),
        (BreakoutParams(max_stop_pct=0.001), WIDE_STOP),
    ],
)
def test_breakout_filters_say_why_they_discarded_it(params: BreakoutParams, reason: str) -> None:
    last = _last(pair_breakout(), params)
    assert (last["setup"], last["filtered"]) == ("", reason)
    assert np.isnan(last["level"])


def test_breakout_adx_variant_wants_strength_or_a_rising_adx() -> None:
    features = _local(pair_breakout())
    falling = features.copy()
    falling.loc[falling.index[-2:], "adx"] = [15.0, 14.0]
    params = BreakoutParams(adx_filter=True)
    last = local_frame(falling, params).iloc[-1]
    assert (last["setup"], last["filtered"]) == ("", NO_ADX)
    falling.loc[falling.index[-1], "adx"] = 16.0  # rising again
    assert local_frame(falling, params).iloc[-1]["setup"] == PAIR_BREAKOUT


def test_breakout_filter_ablations_and_the_squeeze() -> None:
    frame = pair_breakout()
    off = BreakoutParams(max_extension_atr=0, max_range_atr=0, adx_filter=True, adx_min=0)
    assert _last(frame, off)["setup"] == PAIR_BREAKOUT
    # without a squeeze before it, a breakout is no setup (``breakout`` has no compression)
    assert (local_frame(_local(breakout()), BreakoutParams())["setup"] == "").all()


def _retest(frame: pd.DataFrame, closes: list[float]) -> pd.DataFrame:
    return ohlcv([*frame["close"], *closes], volumes=[*frame["volume"], *[100.0] * len(closes)])


def test_breakout_retest_waits_for_a_candle_that_holds_the_level() -> None:
    frame = pair_breakout()
    last = frame["close"].iloc[-1]
    params = BreakoutParams(retest=True)
    assert _last(frame, params)["setup"] == ""  # the breakout candle only arms it
    held = _local(_retest(frame, [last * 1.001]))
    signal = evaluate_local(held, params)
    level = _last(frame, BreakoutParams())["level"]
    assert signal.setup == PAIR_BREAKOUT
    assert signal.level == pytest.approx(level)
    # a dip that closes below the level is no retest; four candles later it has expired
    assert _last(_retest(frame, [last * 0.99]), params)["setup"] == ""
    assert _last(_retest(frame, [last * 1.0005] * 3 + [last * 1.002]), params)["setup"] == ""


def test_breakout_exits_below_the_level_its_entry_broke() -> None:
    signal = evaluate_local(_local(pair_breakout()), BreakoutParams())
    assert signal.level is not None
    params = BreakoutParams()
    expected = TECHNICAL_EXITS[Family.PAIR_VOLATILITY_BREAKOUT]
    assert position_exit(signal, params, signal.close * 1.01) == expected
    assert position_exit(signal, params, signal.level) is None
    assert position_exit(signal, params, None) is None


def test_range_target_is_the_middle_band_frozen_on_the_signal() -> None:
    features = _local(pair_range())
    signal = evaluate_local(features, RangeParams())
    middle = features["bb_middle"].iloc[-1]
    assert signal.target_pct == pytest.approx(middle / signal.close - 1)
    # a middle band below the close is no target
    below = features.copy()
    below.loc[below.index[-1], "bb_middle"] = signal.close * 0.99
    last = local_frame(below, RangeParams()).iloc[-1]
    assert (last["setup"], last["filtered"]) == ("", NO_TARGET)
    assert _last(pair_range(), RangeParams(max_stop_pct=0.001))["filtered"] == WIDE_STOP


def test_range_exits_on_a_close_below_the_lower_band() -> None:
    frame = pair_range()
    falling = ohlcv([*frame["close"], frame["close"].iloc[-1] * 0.97])
    signal = evaluate_local(_local(falling), RangeParams())
    assert signal.exit_signal
    assert (
        position_exit(signal, RangeParams(), None) == TECHNICAL_EXITS[Family.PAIR_RANGE_REVERSION]
    )


def test_evaluate_local_neutral_values_and_errors() -> None:
    features = _local(pair_pullback())
    nan = features.copy()
    nan["atr_pct"] = np.nan
    nan["atr"] = np.nan
    signal = evaluate_local(nan, PullbackParams())
    assert (signal.setup, signal.stop_pct, signal.atr_pct) == (None, 0.06, 0.0)
    with pytest.raises(ValueError, match="sem candles"):
        evaluate_local(features.iloc[:0], PullbackParams())


def test_local_regime_and_family_of() -> None:
    regimes = local_regime_frame(_local(pair_pullback()))
    assert regimes.iloc[-60] == "alta"
    assert set(regimes) <= {"alta", "baixa", "lateral"}
    assert local_regime_frame(_local(ohlcv(np.linspace(200, 100, 300)))).iloc[-1] == "baixa"
    assert local_regime_frame(_local(ohlcv([100.0] * 300))).iloc[-1] == "lateral"
    assert family_of(PullbackParams()) is Family.PAIR_PULLBACK_RSI
    assert family_of(BreakoutParams()) is Family.PAIR_VOLATILITY_BREAKOUT
    assert family_of(RangeParams()) is Family.PAIR_RANGE_REVERSION
    assert family_of(SignalParams()) is Family.LEGACY
    assert [f.local for f in Family] == [False, True, True, True]


def test_local_score_drops_only_the_relative_strength() -> None:
    asset = uptrend_with_pullback()
    weak = compute_features(asset, benchmark_close=asset["close"] * 2.0 ** np.arange(len(asset)))
    legacy = score_frame(weak, SignalParams())
    local = score_frame(weak, SignalParams(local_score=True))
    unclipped = weak["rs_roc"].notna() & (legacy > -1)
    assert unclipped.sum() > 100
    np.testing.assert_allclose((local - legacy)[unclipped].to_numpy(), 0.20)
    assert local.max() <= 0.80


def _benchmark(index: pd.Index, slope: float) -> pd.Series:
    return pd.Series(100 * np.exp(slope * np.arange(len(index))), index=index)


@pytest.mark.parametrize(("frame", "params", "setup"), CASES)
def test_the_benchmark_cannot_change_score_setup_stop_target_or_exit(
    frame: pd.DataFrame, params: LocalParams, setup: str
) -> None:
    """BTC's trajectory, its side of the EMA 200 and its absence (D-044, spec §11)."""
    variants = {
        "absent": None,
        "above": _benchmark(frame.index, 0.002),
        "below": _benchmark(frame.index, -0.002),
        "crash": _benchmark(frame.index, 0.0).where(np.arange(len(frame)) < 250, 10.0),
    }
    features = {name: _local(frame, bench) for name, bench in variants.items()}
    # the benchmark did change the legacy columns...
    assert features["above"]["regime_up"].iloc[-1] and not features["below"]["regime_up"].iloc[-1]
    assert not features["above"]["rs_roc"].equals(features["below"]["rs_roc"])
    # ...and nothing the family decides
    frames = {name: local_frame(f, params) for name, f in features.items()}
    for name in ("above", "below", "crash"):
        pd.testing.assert_frame_equal(frames[name], frames["absent"])
        assert evaluate_local(features[name], params) == evaluate_local(features["absent"], params)
    assert frames["absent"]["setup"].iloc[-1] == setup


@pytest.mark.parametrize(("frame", "params", "setup"), CASES)
def test_999_candles_decide_like_the_whole_history(
    frame: pd.DataFrame, params: LocalParams, setup: str
) -> None:
    """The agent computes on 999 candles; the lab on the whole history. Prefixing 2000 candles
    of a different past changes neither the setups nor the values of the last 500 candles."""
    rng = np.random.default_rng(9)
    past = frame["close"].iloc[0] * np.exp(np.cumsum(rng.normal(0, 0.01, 2000)))
    past *= frame["close"].iloc[0] / past[-1]
    longer = ohlcv([*past, *frame["close"]], volumes=[*[100.0] * 2000, *frame["volume"]])
    tail = 500
    short = local_frame(
        _local(
            ohlcv(
                longer["close"].iloc[-999:].to_numpy(),
                volumes=longer["volume"].iloc[-999:].to_numpy(),
            )
        ),
        params,
    )
    full = local_frame(_local(longer), params)
    assert list(short["setup"].iloc[-tail:]) == list(full["setup"].iloc[-tail:])
    for column in ("stop_pct", "score"):
        np.testing.assert_allclose(
            short[column].iloc[-tail:].to_numpy(), full[column].iloc[-tail:].to_numpy(), rtol=1e-9
        )
    assert full["setup"].iloc[-1] == setup


def test_a_recent_listing_with_only_the_warmup_still_evaluates() -> None:
    frame = pair_pullback().iloc[-210:]
    signal = evaluate_local(_local(frame), replace(PullbackParams()))
    assert signal.local_regime in {"alta", "baixa", "lateral"}
