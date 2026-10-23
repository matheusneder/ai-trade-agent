"""The lab's "shell" strategy: backtest/hyperopt of the agent's signals in Freqtrade.

Imports the **same** signals package used in production (``trade_agent.signals``, mounted
into the container through ``PYTHONPATH``) and replicates the agent's native protection:

* fixed stop at ``entry × (1 − stop_pct)``, with ``stop_pct`` from the ATR capped at the
  profile's maximum (the OCO's ``STOP_LOSS`` leg);
* *trailing take-profit*: on reaching ``+tp_activation``, the stop starts following the top
  at a ``tp_trailing`` distance (``TAKE_PROFIT`` leg with ``trailingDelta``); with
  ``tp_activation_atr_mult``, the activation is that multiple of the entry's ATR%, clipped to
  [``tp_activation``, ``tp_activation_max``];
* the Risk Guard's minimum R:R: no entry when ``(activation − fee) / (stop + fee)`` is below
  ``min_reward_risk`` (``pre_trade`` in ``config/stop_conditions.yaml``);
* risk-based size: ``wallet × risk_per_trade / stop_pct``, capped per position;
* the agent's decision exits: stop at *break-even* after ``break_even_r`` and the maximum
  holding time;
* with ``universe_min_volume`` (``--alts``), entries only while the pair's 24h quote volume
  reaches the universe's minimum, as the agent only sees a pair inside its universe.

The default parameters are only a starting point: ``lab/walk_forward.py`` generates the
``trade_agent_strategy.json`` file with the values of the profile chosen in
``config/profiles.yaml``.
"""

import math
from datetime import datetime, timedelta
from typing import Any

from freqtrade.data.converter import trim_dataframe, trim_dataframes
from freqtrade.optimize import backtesting
from freqtrade.persistence import Trade
from freqtrade.strategy import (
    DecimalParameter,
    IStrategy,
    stoploss_from_open,
    timeframe_to_minutes,
    timeframe_to_prev_date,
)
from pandas import DataFrame

from trade_agent.signals import (
    FeatureParams,
    SignalParams,
    compute_features,
    score_frame,
    setup_frame,
    stop_pct_frame,
)

BREAK_EVEN_FEE_BUFFER = 0.003
"""Same margin above the entry as ``trade_agent.strategy.exits.DEFAULT_FEE_BUFFER``."""
RATIO_TOLERANCE = 1e-9


# Freqtrade drops ``startup_candle_count`` rows from the start of each pair's data (by count),
# not the rows before the window's start date. With 999 candles (D-040), a pair with less
# history before a window would lose the window's first candles, while the agent evaluates any
# asset with ``FeatureParams.warmup`` candles. Both backtesting call sites pass the window's
# timerange, so trimming with ``startup_candles=0`` cuts by its start date instead: identical
# for pairs with the full history, and recent listings trade from the window's start.
def _trim_dataframe_by_date(
    df: DataFrame, timerange: Any, *, df_date_col: str = "date", startup_candles: int = 0
) -> DataFrame:
    return trim_dataframe(df, timerange, df_date_col=df_date_col)


def _trim_dataframes_by_date(
    preprocessed: dict[str, DataFrame], timerange: Any, startup_candles: int
) -> dict[str, DataFrame]:
    return trim_dataframes(preprocessed, timerange, 0)


backtesting.trim_dataframe = _trim_dataframe_by_date  # type: ignore[assignment]
backtesting.trim_dataframes = _trim_dataframes_by_date  # type: ignore[assignment]


class TradeAgentStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    minimal_roi = {"0": 100}  # noqa: RUF012 - no ROI: exits by stop, trailing TP and rotation
    stoploss = -0.25  # safety net; the real stop comes from custom_stoploss
    use_custom_stoploss = True
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    # Freqtrade loads only these candles before each window: the agent's history (D-040), not
    # the warmup, or the EMA 200 starts every window at its SMA seed.
    startup_candle_count = FeatureParams().history
    benchmark_pair = "BTC/USDT"

    # ------------------------------------------------------------------ entry (space=buy)
    min_score = DecimalParameter(0.2, 0.9, default=0.6, decimals=2, space="buy")
    adx_min = DecimalParameter(10, 35, default=20, decimals=0, space="buy")
    rsi_pullback_max = DecimalParameter(30, 55, default=45, decimals=0, space="buy")
    vol_rel_min = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="buy")
    risk_per_trade = DecimalParameter(
        0.001, 0.03, default=0.005, decimals=3, space="buy", optimize=False
    )
    max_position_pct = DecimalParameter(
        0.05, 1.0, default=0.25, decimals=2, space="buy", optimize=False
    )
    # Risk Guard pre-trade check (``PreTradeConfig``); 0 = off.
    min_reward_risk = DecimalParameter(0, 5, default=0, decimals=2, space="buy", optimize=False)
    round_trip_fee = DecimalParameter(
        0, 0.01, default=0.002, decimals=4, space="buy", optimize=False
    )
    execution_slippage = DecimalParameter(
        0, 0.1, default=0, decimals=4, space="buy", optimize=False
    )
    cost_adjusted = DecimalParameter(0, 1, default=0, decimals=0, space="buy", optimize=False)
    trailing_floor = DecimalParameter(0, 1, default=0, decimals=0, space="buy", optimize=False)
    # ``signals.min_stop_atr`` of the profile (``SignalParams``); 0 = off.
    min_stop_atr = DecimalParameter(0, 10, default=0, decimals=2, space="buy", optimize=False)
    # Universe emulation: the agent only sees a pair while its 24h quote volume reaches
    # ``UniverseConfig.min_quote_volume_24h`` (``--alts``); 0 = off (static basket).
    universe_min_volume = DecimalParameter(
        0, 1e10, default=0, decimals=0, space="buy", optimize=False
    )

    # ------------------------------------------------------------------ exit (space=sell)
    # Parameters with optimize=False define the profile's risk and are never optimized.
    atr_stop_mult = DecimalParameter(1.0, 4.0, default=2.0, decimals=1, space="sell")
    stop_max_pct = DecimalParameter(
        0.02, 0.20, default=0.04, decimals=3, space="sell", optimize=False
    )
    tp_activation = DecimalParameter(0.01, 0.25, default=0.03, decimals=3, space="sell")
    tp_trailing = DecimalParameter(0.005, 0.05, default=0.01, decimals=3, space="sell")
    # ATR-based activation (``take_profit.activation_atr_mult``), from the profile; 0 = off.
    tp_activation_atr_mult = DecimalParameter(
        0, 20, default=0, decimals=2, space="sell", optimize=False
    )
    tp_activation_max = DecimalParameter(
        0, 0.5, default=0, decimals=3, space="sell", optimize=False
    )
    exit_score = DecimalParameter(-0.8, 0.2, default=-0.2, decimals=2, space="sell")
    # Decision exits of the agent (``trade_agent.strategy.exits``), from the profile; 0 = off.
    break_even_r = DecimalParameter(0, 5, default=0, decimals=2, space="sell", optimize=False)
    max_holding_hours = DecimalParameter(
        0, 2000, default=0, decimals=0, space="sell", optimize=False
    )

    def informative_pairs(self) -> list[tuple[str, str]]:
        return [(self.benchmark_pair, self.timeframe)]

    def _signal_params(self) -> SignalParams:
        return SignalParams(
            adx_min=float(self.adx_min.value),
            rsi_pullback_max=float(self.rsi_pullback_max.value),
            vol_rel_min=float(self.vol_rel_min.value),
            atr_stop_mult=float(self.atr_stop_mult.value),
            max_stop_pct=float(self.stop_max_pct.value),
            min_stop_atr=float(self.min_stop_atr.value) or None,
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        benchmark = None
        if metadata["pair"] != self.benchmark_pair and self.dp is not None:
            bench = self.dp.get_pair_dataframe(self.benchmark_pair, self.timeframe)
            if not bench.empty:
                benchmark = bench.set_index("date")["close"]
        features = compute_features(dataframe.set_index("date"), benchmark_close=benchmark)
        return features.reset_index()

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        # Score, setup and stop depend on optimizable parameters: they are computed here because
        # hyperopt runs populate_indicators only once and this method on every epoch.
        params = self._signal_params()
        dataframe["score"] = score_frame(dataframe, params)
        dataframe["setup"] = setup_frame(dataframe, params)
        dataframe["stop_pct"] = stop_pct_frame(dataframe, params)
        fee = float(self.round_trip_fee.value) + float(self.execution_slippage.value)
        activation = dataframe["atr_pct"].map(self._activation_for)
        if self.trailing_floor.value:
            activation = (1 + activation) * (1 - float(self.tp_trailing.value)) - 1
        reward_risk = (activation - fee) / (dataframe["stop_pct"] + fee)
        entry = (
            (dataframe["setup"] != "")
            & (dataframe["score"] >= float(self.min_score.value))
            & (dataframe["volume"] > 0)
            # tolerance: the Risk Guard compares Decimals, where 10.8 / 7.2 is exactly 1.5
            & (reward_risk >= float(self.min_reward_risk.value) - RATIO_TOLERANCE)
        )
        min_volume = float(self.universe_min_volume.value)
        if min_volume > 0:
            per_day = 1440 // timeframe_to_minutes(self.timeframe)
            quote_24h = (dataframe["close"] * dataframe["volume"]).rolling(per_day).sum()
            entry &= quote_24h >= min_volume
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = dataframe.loc[entry, "setup"]
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        dataframe.loc[dataframe["score"] <= float(self.exit_score.value), "exit_long"] = 1
        return dataframe

    def _entry_value(self, pair: str, trade: Trade, column: str, default: float) -> float:
        """``column`` on the signal candle of the entry (``default`` if missing)."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        candle = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        rows = dataframe.loc[dataframe["date"] == candle]
        if rows.empty:
            return default
        value = float(rows[column].iloc[-1])
        return default if math.isnan(value) else value

    def _entry_stop_pct(self, pair: str, trade: Trade) -> float:
        return self._entry_value(pair, trade, "stop_pct", float(self.stop_max_pct.value))

    def _activation_for(self, atr_pct: float) -> float:
        """Same rule as ``TakeProfitConfig.activation_for`` (fractions instead of %)."""
        floor = float(self.tp_activation.value)
        mult = float(self.tp_activation_atr_mult.value)
        if mult <= 0 or math.isnan(atr_pct) or atr_pct <= 0:
            return floor
        return min(max(mult * atr_pct, floor), float(self.tp_activation_max.value))

    def _tp_activation(self, pair: str, trade: Trade) -> float:
        return self._activation_for(self._entry_value(pair, trade, "atr_pct", 0.0))

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        max_rate = trade.max_rate or trade.open_rate
        if max_rate >= trade.open_rate * (1 + self._tp_activation(pair, trade)):
            return -float(self.tp_trailing.value)  # trailing TP activated: follows the top
        stop_pct = self._entry_stop_pct(pair, trade)
        if self._break_even_reached(pair, trade, stop_pct):
            return stoploss_from_open(
                BREAK_EVEN_FEE_BUFFER, current_profit, is_short=False, leverage=trade.leverage
            )
        return stoploss_from_open(
            -stop_pct, current_profit, is_short=False, leverage=trade.leverage
        )

    def _break_even_reached(self, pair: str, trade: Trade, stop_pct: float) -> bool:
        """The agent checks *break-even* once per cycle (candle close), not on the intra-candle
        high: the last closed candle's close at ``break_even_r`` × the stop distance."""
        trigger_r = float(self.break_even_r.value)
        if trigger_r <= 0:
            return False
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return False
        last = dataframe.iloc[-1]
        if last["date"] < timeframe_to_prev_date(self.timeframe, trade.open_date_utc):
            return False
        return float(last["close"]) >= trade.open_rate * (1 + stop_pct * trigger_r)

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        hours = float(self.max_holding_hours.value)
        if hours > 0 and current_time - trade.open_date_utc >= timedelta(hours=hours):
            return "max_holding"
        return None

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stop_pct = (
            float(dataframe["stop_pct"].iloc[-1])
            if not dataframe.empty
            else float(self.stop_max_pct.value)
        )
        wallet = self.wallets.get_total_stake_amount()
        if self.cost_adjusted.value:
            stop_pct += float(self.round_trip_fee.value) + float(self.execution_slippage.value)
        stake = wallet * float(self.risk_per_trade.value) / max(stop_pct, 1e-6)
        return min(stake, wallet * float(self.max_position_pct.value), max_stake)
