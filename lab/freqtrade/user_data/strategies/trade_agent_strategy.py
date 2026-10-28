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

The pair-local families (D-044, ``trade_agent.signals.local``) read only the pair's candles
(no benchmark in the features) and add, like the agent's planner and the profile's ``risk``:

* the structural stop of the signal candle, an activation ``r × (stop + cost) + cost``
  (``activation_r``), a trailing of ``trailing_atr_mult`` ATRs and a fixed target
  (``limit``: a native limit order, simulated as Freqtrade's ROI at the target price);
* the family's technical exit at the candle close (the breakout's against the level its
  entry broke), and no entry once the price ran past the signal's ``entry_cap``;
* the scope's budget (:mod:`trade_agent.risk.scope`): daily marked-to-market loss, losing
  streak, drawdown review, re-entry after a stop and the cap on the open initial risk.

With ``capital_model`` ``operational`` the size comes from the profile's configured capital,
as in the agent, instead of Freqtrade's compounding wallet.

``lab/walk_forward.py`` writes the profile's values: Freqtrade's parameters
(``trade_agent_strategy.json``, which *hyperopt* may change) and the rest of the profile
(``trade_agent_profile.json``). The strategy refuses to run without the latter and writes
back the values it used (``trade_agent_effective.json``). Freqtrade swallows the exceptions
of some callbacks, so those go to ``trade_agent_events.jsonl``, which the lab checks.
"""

import functools
import json
import math
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
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

from trade_agent.risk.scope import ScopeLimits, ScopeRisk
from trade_agent.signals import (
    Family,
    FeatureParams,
    SignalParams,
    compute_features,
    compute_local_features,
    local_frame,
    score_frame,
    setup_frame,
    stop_pct_frame,
)
from trade_agent.signals.local import PARAMS as LOCAL_PARAMS
from trade_agent.signals.local import LocalParams

BREAK_EVEN_FEE_BUFFER = 0.003
"""Same margin above the entry as ``trade_agent.strategy.exits.DEFAULT_FEE_BUFFER``."""
RATIO_TOLERANCE = 1e-9
PROFILE_FILE = Path(__file__).with_name("trade_agent_profile.json")
EFFECTIVE_FILE = Path(__file__).with_name("trade_agent_effective.json")
EVENTS_FILE = Path(__file__).with_name("trade_agent_events.jsonl")
MIN_TRAILING_BIPS, MAX_TRAILING_BIPS = 10, 2000
"""``trade_agent.strategy.profiles``: Binance's ``trailingDelta`` range."""
LOCAL_COLUMNS = ("setup", "score", "stop_pct", "target_pct", "entry_cap", "level")


# Freqtrade drops ``startup_candle_count`` rows from the start of each pair's data (by count),
# not the rows before the window's start date. With 999 candles (D-040), a pair with less
# history before a window would lose the window's first candles, while the agent evaluates any
# asset with ``FeatureParams.warmup`` candles. Both backtesting call sites pass the window's
# timerange, so trimming with ``startup_candles=0`` cuts by its start date instead: identical
# for pairs with the full history, and recent listings trade from the window's start. The
# callbacks' cursor still assumes the count trim, so they read candles through
# ``_closed_candles`` (cut by time), never the cursor's last row.
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


def _event(kind: str, **data: Any) -> None:
    with EVENTS_FILE.open("a", encoding="utf-8") as events:
        events.write(json.dumps({"kind": kind, **data}, default=str) + "\n")


def _logged[**P, R](callback: Callable[P, R]) -> Callable[P, R]:
    """Freqtrade swallows the exceptions of most callbacks (returning a default): log them to
    the events file, which the lab checks, before they go."""

    @functools.wraps(callback)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            _event("error", callback=callback.__name__, error=repr(exc))
            raise

    return wrapper


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
    # Freqtrade rounds a loaded value to ``decimals``: 4 keeps the 0.25% of pair_range_1h
    risk_per_trade = DecimalParameter(
        0.001, 0.03, default=0.005, decimals=4, space="buy", optimize=False
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
    # Universe emulation: the agent only sees a pair while its 24h quote volume reaches
    # ``UniverseConfig.min_quote_volume_24h`` (``--alts``); 0 = off (static basket).
    universe_min_volume = DecimalParameter(
        0, 1e10, default=0, decimals=0, space="buy", optimize=False
    )

    # ------------------------------------------------------------------ exit (space=sell)
    # Parameters with optimize=False define the profile's risk and are never optimized. The
    # ranges are *hyperopt*'s: a profile's value outside them is still loaded as it is.
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

    # ------------------------------------------------------------------ the profile
    def _profile(self) -> dict[str, Any]:
        """``trade_agent_profile.json``: every value Freqtrade's parameters do not carry."""
        cached: dict[str, Any] | None = getattr(self, "_manifest", None)
        if cached is None:
            cached = self._manifest = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        return cached

    def _family(self) -> Family:
        return Family(self._profile()["family"])

    def _signal_params(self) -> SignalParams:
        """The legacy family's parameters: the profile's, with the ones *hyperopt* may
        change taken from Freqtrade's parameters."""
        return replace(
            SignalParams(**self._profile()["signals"]),
            adx_min=float(self.adx_min.value),
            rsi_pullback_max=float(self.rsi_pullback_max.value),
            vol_rel_min=float(self.vol_rel_min.value),
            atr_stop_mult=float(self.atr_stop_mult.value),
            max_stop_pct=float(self.stop_max_pct.value),
        )

    def _local_params(self) -> LocalParams:
        return LOCAL_PARAMS[self._family()](**self._profile()["signals"])

    def _parameters(self) -> dict[str, dict[str, float]]:
        spaces: dict[str, dict[str, float]] = {"buy": {}, "sell": {}}
        for name, attribute in vars(type(self)).items():
            if isinstance(attribute, DecimalParameter) and attribute.space in spaces:
                spaces[attribute.space][name] = float(attribute.value)
        return spaces

    @_logged
    def bot_start(self, **kwargs: Any) -> None:
        EVENTS_FILE.write_text("", encoding="utf-8")
        profile = self._profile()
        family = self._family()
        take_profit = profile["take_profit"]
        self.use_custom_roi = family.local and take_profit["mode"] == "limit"
        self._closed: set[int] = set()
        self._scope: ScopeRisk | None = None
        self._recorded = False
        risk = profile["risk"]
        if family.local and risk is not None:
            candle = timedelta(minutes=timeframe_to_minutes(self.timeframe))
            self._scope = ScopeRisk(
                ScopeLimits(
                    capital=float(profile["capital"]),
                    daily_loss_pct=risk["daily_loss_pct"],
                    consecutive_losses=risk["consecutive_losses"],
                    cooldown=timedelta(hours=risk["cooldown_hours"]),
                    max_drawdown_pct=risk["max_drawdown_pct"],
                    reentry=candle * risk["reentry_candles"],
                )
            )

    def _record_effective(self) -> None:
        """Writes the values in use, once: Freqtrade loads its parameters after ``bot_start``."""
        if getattr(self, "_recorded", True):
            return
        self._recorded = True
        profile = self._profile()
        params = self._local_params() if self._family().local else self._signal_params()
        effective = {
            "params": self._parameters(),
            "profile": {**profile, "signals": asdict(params)},
        }
        EFFECTIVE_FILE.write_text(json.dumps(effective, indent=2), encoding="utf-8")

    def informative_pairs(self) -> list[tuple[str, str]]:
        return [(self.benchmark_pair, self.timeframe)]

    # ------------------------------------------------------------------ signals
    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        if self._family().local:  # the pair's own candles only (D-044)
            features = compute_features(dataframe.set_index("date"))
            return compute_local_features(features).reset_index()
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
        self._record_effective()
        fee = float(self.round_trip_fee.value) + float(self.execution_slippage.value)
        if self._family().local:
            frame = local_frame(dataframe, self._local_params())
            for column in LOCAL_COLUMNS:
                dataframe[column] = frame[column]
            dataframe["exit_local"] = frame["exit"]
            activation = self._local_activation(
                dataframe["stop_pct"], dataframe["atr_pct"], dataframe["target_pct"]
            )
            if self.trailing_floor.value and self._profile()["take_profit"]["mode"] == "trailing":
                activation = (1 + activation) * (1 - self._trailing(dataframe["atr_pct"])) - 1
        else:
            params = self._signal_params()
            dataframe["score"] = score_frame(dataframe, params)
            dataframe["setup"] = setup_frame(dataframe, params)
            dataframe["stop_pct"] = stop_pct_frame(dataframe, params)
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
        if self._family().local:  # the family's rule on the closed candle (the breakout's
            # depends on the entry: custom_exit)
            dataframe.loc[dataframe["exit_local"].astype(bool), "exit_long"] = 1
            return dataframe
        dataframe.loc[dataframe["score"] <= float(self.exit_score.value), "exit_long"] = 1
        return dataframe

    # ------------------------------------------------------------------ the entry's values
    def _entry_value(self, pair: str, trade: Trade, column: str, default: float) -> float:
        """``column`` on the entry's signal candle (``default`` if missing).

        That is the last candle closed when the entry was sent, as in the agent, which sizes
        the position and places the OCO with that signal's stop, fixed from then on. It is
        the same candle ``custom_stake_amount`` sizes with. The entry's own candle is still
        open at the entry: reading it (``timeframe_to_prev_date(open_date)``) would either
        miss it (with the cursor aligned) or see its final high and low (cursor ahead).
        """
        rows = self._closed_candles(pair, trade.open_date_utc)
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

    def _cost(self) -> float:
        """The profile's round-trip cost (fraction) in the sizing and in ``activation_r``."""
        risk = self._profile()["risk"]
        if risk is not None:
            return float(risk["cost_pct"]) / 100
        if self.cost_adjusted.value:
            return float(self.round_trip_fee.value) + float(self.execution_slippage.value)
        return 0.0

    def _local_activation(self, stop_pct: Any, atr_pct: Any, target_pct: Any) -> Any:
        """``ProtectionConfig.policy`` of a pair-local family (fractions instead of %), on
        floats or Series."""
        take_profit = self._profile()["take_profit"]
        if isinstance(atr_pct, float):
            activation: Any = self._activation_for(atr_pct)
        else:
            activation = atr_pct.map(self._activation_for)
        if take_profit["activation_r"] is not None:
            cost = self._cost()
            capped = np.minimum(stop_pct, float(self.stop_max_pct.value))
            activation = np.maximum(
                activation, take_profit["activation_r"] * (capped + cost) + cost
            )
        if take_profit["signal_target"]:
            activation = np.maximum(activation, np.nan_to_num(target_pct, nan=0.0))
        return activation

    def _trailing(self, atr_pct: Any) -> Any:
        """``TakeProfitConfig.trailing_bips_for`` (a fraction): ATR-based, rounded to bips
        and clipped to Binance's range, or the fixed one."""
        mult = self._profile()["take_profit"]["trailing_atr_mult"]
        if mult is None:
            return float(self.tp_trailing.value)
        bips = np.clip(np.round(mult * np.nan_to_num(atr_pct, nan=0.0) * 10_000), 10, 2000)
        return bips / 10_000

    def _local_target(self, pair: str, trade: Trade) -> float:
        stop = self._entry_stop_pct(pair, trade)
        atr = self._entry_value(pair, trade, "atr_pct", 0.0)
        target = self._entry_value(pair, trade, "target_pct", math.nan)
        return float(self._local_activation(stop, atr, target))

    # ------------------------------------------------------------------ protection and exits
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
        # Not decorated: Freqtrade reads ``after_fill`` from this signature
        # (``getfullargspec``, which ignores ``functools.wraps``). Without it, Freqtrade skips
        # the stop placed right after the fill, and the next adjustment marks every stop as a
        # trailing one (``trailing_stop_loss``), at the same prices.
        try:
            return self._stoploss(pair, trade, current_time, current_profit)
        except Exception as exc:
            _event("error", callback="custom_stoploss", error=repr(exc))
            raise

    def _stoploss(
        self, pair: str, trade: Trade, current_time: datetime, current_profit: float
    ) -> float:
        max_rate = trade.max_rate or trade.open_rate
        if self._family().local:
            stop_pct = self._entry_stop_pct(pair, trade)
            if self._profile()["take_profit"]["mode"] == "trailing":
                atr = self._entry_value(pair, trade, "atr_pct", 0.0)
                if max_rate >= trade.open_rate * (1 + self._local_target(pair, trade)):
                    return -float(self._trailing(atr))
            return stoploss_from_open(
                -stop_pct, current_profit, is_short=False, leverage=trade.leverage
            )
        if max_rate >= trade.open_rate * (1 + self._tp_activation(pair, trade)):
            return -float(self.tp_trailing.value)  # trailing TP activated: follows the top
        stop_pct = self._entry_stop_pct(pair, trade)
        if self._break_even_reached(pair, trade, stop_pct, current_time):
            return stoploss_from_open(
                BREAK_EVEN_FEE_BUFFER, current_profit, is_short=False, leverage=trade.leverage
            )
        return stoploss_from_open(
            -stop_pct, current_profit, is_short=False, leverage=trade.leverage
        )

    def _tp_activation(self, pair: str, trade: Trade) -> float:
        return self._activation_for(self._entry_value(pair, trade, "atr_pct", 0.0))

    @_logged
    def custom_roi(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        trade_duration: int,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float | None:
        """The fixed target of a pair-local family (``limit``): a native limit order fills
        once the price touches it, so Freqtrade's ROI, whose exit fills at its price, models it.
        The ROI is net of fees: the one whose close price is the target."""
        target = self._local_target(pair, trade)
        return (1 + target) * (1 - trade.fee_close) / (1 + trade.fee_open) - 1

    def _closed_candles(self, pair: str, current_time: datetime) -> DataFrame:
        """The analyzed candles already closed at ``current_time``, never later ones.

        Freqtrade caches the analyzed dataframe before the startup trim and points the
        callbacks' cursor at ``startup_candle_count + row``. With the trim by date, a pair with
        less history before the window loses fewer rows, so that cursor runs ahead of time.
        Cutting by the candle's close time keeps every callback causal whatever the cursor;
        for a pair with the full history it is the same last candle as before.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return dataframe
        span = timedelta(minutes=timeframe_to_minutes(self.timeframe))
        return dataframe.loc[dataframe["date"] <= current_time - span]

    def _break_even_reached(
        self, pair: str, trade: Trade, stop_pct: float, current_time: datetime
    ) -> bool:
        """The agent checks *break-even* once per cycle (candle close), not on the intra-candle
        high: the last closed candle's close at ``break_even_r`` × the stop distance."""
        trigger_r = float(self.break_even_r.value)
        if trigger_r <= 0:
            return False
        dataframe = self._closed_candles(pair, current_time)
        if dataframe.empty:
            return False
        last = dataframe.iloc[-1]
        if last["date"] < timeframe_to_prev_date(self.timeframe, trade.open_date_utc):
            return False
        return float(last["close"]) >= trade.open_rate * (1 + stop_pct * trigger_r)

    @_logged
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
        if self._family() is Family.PAIR_VOLATILITY_BREAKOUT:
            level = self._entry_value(pair, trade, "level", math.nan)
            rows = self._closed_candles(pair, current_time)
            if not rows.empty and float(rows["close"].iloc[-1]) < level:
                return "technical_exit"  # closed back below the level the entry broke
        return None

    # ------------------------------------------------------------------ entries and size
    def _open_trades(self) -> list[Trade]:
        return list(Trade.get_trades_proxy(is_open=True))

    def _sync_closed(self) -> None:
        """Feeds the scope with the trades closed since the last call, in order."""
        if self._scope is None:
            return
        closed = [t for t in Trade.get_trades_proxy(is_open=False) if t.id not in self._closed]
        for trade in sorted(closed, key=lambda t: t.close_date_utc):
            self._closed.add(trade.id)
            stop = trade.exit_reason == "stop_loss"
            self._scope.closed(
                trade.close_date_utc, trade.pair, float(trade.close_profit_abs or 0), stop=stop
            )
        self._emit_scope_events()

    def _emit_scope_events(self) -> None:
        if self._scope is None:
            return
        emitted = getattr(self, "_emitted", 0)
        for at, reason in self._scope.events[emitted:]:
            _event("scope", at=at, reason=reason)
        self._emitted = len(self._scope.events)

    @_logged
    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        """Marks the scope's equity to market at each candle (closed candles only)."""
        scope = getattr(self, "_scope", None)
        if scope is None:
            return
        self._sync_closed()
        closed = sum(float(t.close_profit_abs or 0) for t in Trade.get_trades_proxy(is_open=False))
        open_ = 0.0
        for trade in self._open_trades():
            rows = self._closed_candles(trade.pair, current_time)
            rate = float(rows["close"].iloc[-1]) if not rows.empty else trade.open_rate
            open_ += trade.calc_profit(rate)
        scope.mark(current_time, float(self._profile()["capital"]) + closed + open_)
        self._emit_scope_events()

    @_logged
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        if not self._family().local:
            return True
        return self._local_entry_allowed(pair, amount, rate, current_time)

    def _local_entry_allowed(
        self, pair: str, amount: float, rate: float, current_time: datetime
    ) -> bool:
        # The agent sends the entry once, right after the signal candle closes. With a detail
        # timeframe Freqtrade retries a refused entry on every detail candle of the next one,
        # which would buy later, at another price: only its first detail candle may enter.
        candle = timeframe_to_minutes(self.timeframe) * 60
        if int(current_time.timestamp()) % candle:
            return False
        rows = self._closed_candles(pair, current_time)  # the entry's signal candle
        cap = float(rows["entry_cap"].iloc[-1])
        if not math.isnan(cap) and rate > cap:
            _event("rejected", at=current_time, pair=pair, reason="entry_cap")
            return False
        if self._scope is not None:
            self._sync_closed()
            reason = self._scope.blocked(current_time, pair)
            if reason is not None:
                _event("rejected", at=current_time, pair=pair, reason=reason)
                return False
        risk = self._profile()["risk"]
        if risk is not None and risk["max_open_risk_pct"] is not None:
            cost = self._cost()
            open_risk = sum(
                t.stake_amount * (self._entry_stop_pct(t.pair, t) + cost)
                for t in self._open_trades()
            )
            new = amount * rate * (float(rows["stop_pct"].iloc[-1]) + cost)
            limit = float(self._profile()["capital"]) * risk["max_open_risk_pct"] / 100
            if open_risk + new > limit * (1 + RATIO_TOLERANCE):
                _event("rejected", at=current_time, pair=pair, reason="open_risk")
                return False
        return True

    @_logged
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
        dataframe = self._closed_candles(pair, current_time)  # the entry's signal candle
        stop_pct = (
            float(dataframe["stop_pct"].iloc[-1])
            if not dataframe.empty
            else float(self.stop_max_pct.value)
        )
        profile = self._profile()
        if profile["capital_model"] == "operational":
            # the agent's planner: the profile's configured capital, its reserve and the
            # positions already open, never the result reinvested
            capital = float(profile["capital"])
            used = sum(t.stake_amount for t in self._open_trades())
            room = capital * (1 - float(profile["cash_reserve"])) - used
            stake = capital * float(self.risk_per_trade.value) / max(stop_pct + self._cost(), 1e-6)
            return max(
                min(stake, capital * float(self.max_position_pct.value), room, max_stake), 0.0
            )
        wallet = self.wallets.get_total_stake_amount()
        if self.cost_adjusted.value:
            stop_pct += float(self.round_trip_fee.value) + float(self.execution_slippage.value)
        stake = wallet * float(self.risk_per_trade.value) / max(stop_pct, 1e-6)
        return min(stake, wallet * float(self.max_position_pct.value), max_stake)
