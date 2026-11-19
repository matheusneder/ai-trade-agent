"""The candle side of ``lead_consensus`` (D-052): the agent's own confirmation of what the
lead traders bought, its stop and its technical exit."""

import numpy as np
import pandas as pd
import pytest

from tests.support.candles import breakout, downtrend, ohlcv, pair_pullback, uptrend_with_pullback
from trade_agent.signals import (
    ConsensusParams,
    Family,
    compute_features,
    compute_local_features,
    evaluate_local,
    family_of,
    local_frame,
    position_exit,
)
from trade_agent.signals.local import (
    BELOW_SHORT,
    DOWNTREND,
    EXTENDED,
    LEAD_CONSENSUS,
    PARAMS,
    TECHNICAL_EXITS,
    WIDE_STOP,
)

PARAMS_ = ConsensusParams()


def _local(frame: pd.DataFrame, benchmark: pd.Series | None = None) -> pd.DataFrame:
    return compute_local_features(compute_features(frame, benchmark_close=benchmark))


def test_the_family_needs_the_lead_traders_and_reads_no_benchmark() -> None:
    assert Family.LEAD_CONSENSUS.local and Family.LEAD_CONSENSUS.needs_leads
    assert [f for f in Family if f.needs_leads] == [Family.LEAD_CONSENSUS]
    assert PARAMS[Family.LEAD_CONSENSUS] is ConsensusParams
    assert family_of(PARAMS_) is Family.LEAD_CONSENSUS


def test_a_pair_holding_above_its_short_average_confirms() -> None:
    features = _local(pair_pullback())
    signal = evaluate_local(features, PARAMS_)
    assert (signal.setup, signal.filtered, signal.exit_signal) == (LEAD_CONSENSUS, None, False)
    assert signal.local_regime == "alta"
    # the stop is structural: below the lows of the last candles, at least 2 ATR away
    last = features.iloc[-1]
    assert signal.stop_pct >= PARAMS_.min_stop_atr * last["atr_pct"]
    assert signal.stop_pct <= PARAMS_.max_stop_pct
    assert signal.target_pct is None and signal.max_entry_price is None and signal.level is None
    assert position_exit(signal, PARAMS_, None) is None


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        (downtrend(), DOWNTREND),
        (uptrend_with_pullback(), BELOW_SHORT),
        (breakout(), EXTENDED),
    ],
)
def test_the_candles_refuse_a_purchase_and_say_why(frame: pd.DataFrame, reason: str) -> None:
    signal = evaluate_local(_local(frame), PARAMS_)
    assert (signal.setup, signal.filtered) == (None, reason)


def test_a_local_downtrend_is_the_technical_exit() -> None:
    signal = evaluate_local(_local(downtrend()), PARAMS_)
    assert signal.exit_signal and signal.local_regime == "baixa"
    assert position_exit(signal, PARAMS_, None) == TECHNICAL_EXITS[Family.LEAD_CONSENSUS]
    assert not evaluate_local(_local(ohlcv([100.0] * 300)), PARAMS_).exit_signal  # sideways


def test_the_extension_check_can_be_turned_off() -> None:
    free = ConsensusParams(max_extension_atr=0)
    assert evaluate_local(_local(breakout()), free).setup == LEAD_CONSENSUS


def test_a_stop_beyond_the_cap_discards_the_setup_instead_of_tightening_it() -> None:
    tight = ConsensusParams(max_stop_pct=0.01)
    signal = evaluate_local(_local(pair_pullback()), tight)
    assert (signal.setup, signal.filtered) == (None, WIDE_STOP)
    assert signal.stop_pct > tight.max_stop_pct  # reported as it is, never tightened to fit


def test_every_candle_is_a_pattern_so_every_refusal_has_a_reason() -> None:
    frame = local_frame(_local(pair_pullback()).iloc[250:], PARAMS_)
    assert ((frame["setup"] != "") ^ (frame["filtered"] != "")).all()
    assert set(frame["setup"]) == {"", LEAD_CONSENSUS}


def test_the_benchmark_cannot_change_the_confirmation() -> None:
    frame = pair_pullback()
    index = frame.index
    falling = pd.Series(100 * np.exp(-0.01 * np.arange(len(index))), index=index)
    alone = local_frame(_local(frame), PARAMS_)
    below = local_frame(_local(frame, falling), PARAMS_)
    pd.testing.assert_frame_equal(alone.drop(columns="score"), below.drop(columns="score"))
    pd.testing.assert_series_equal(alone["score"], below["score"])
