"""The decision engine with a pair-local profile (D-044): it decides on the pair's own candles,
with BTC below its EMA 200 or with no benchmark at all, and exits on its family's rule."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from structlog.testing import capture_logs

from tests.integration.decision.test_engine import (
    FOUR_HOURS,
    Clock,
    _candles,
    _engine,
    _setup_market,
    _strategy_data,
)
from tests.support.candles import START_MS, ohlcv, pair_breakout, pair_pullback, raw_klines
from tests.support.fake_binance import FakeBinance, Fault
from tests.support.local_profiles import LOCAL_PULLBACK, merge
from tests.support.risk import closed_position, snapshot
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceError
from trade_agent.execution.orders import (
    EntryMode,
    EntryOrder,
    ProtectionPolicy,
    StopMode,
    TakeProfitMode,
)
from trade_agent.execution.positions import PositionState
from trade_agent.execution.service import PositionService
from trade_agent.persistence.store import Store
from trade_agent.risk.guard import ScopeSpec
from trade_agent.risk.scope import REENTRY
from trade_agent.signals import Family
from trade_agent.signals.local import TECHNICAL_EXITS
from trade_agent.strategy.profiles import StrategyConfig

D = Decimal
ALIGNED = START_MS - START_MS % FOUR_HOURS
"""Binance's 4h candles open on multiples of 4 hours."""
LOCAL_4H = {"timeframe": "4h", "tiers": {"core": 0.7, "large": 0.7, "mid": 0.7, "small": 0.0}}
POLICY = ProtectionPolicy(
    take_profit_mode=TakeProfitMode.LIMIT,
    take_profit_pct=D(5),
    stop_mode=StopMode.FIXED,
    stop_pct=D(3),
)


def _strategy(**local: Any) -> StrategyConfig:
    data = _strategy_data()
    data["profiles"]["local"] = merge(LOCAL_PULLBACK, merge(LOCAL_4H, local))
    return StrategyConfig.model_validate(data)


def _sol(frame: pd.DataFrame, start_ms: int = START_MS) -> list[list[Any]]:
    return raw_klines(
        ohlcv(
            frame["close"].tolist(),
            volumes=frame["volume"].tolist(),
            interval_ms=FOUR_HOURS,
            start_ms=start_ms,
        )
    )


def _falling_btc(fake: FakeBinance) -> None:
    fake.candles[("BTCUSDT", "4h")] = _candles([100 * math.exp(-0.004 * i) for i in range(320)])


async def test_a_local_profile_buys_with_btc_below_its_ema_200(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake, sol=pair_pullback()["close"].tolist())
    fake.candles[("SOLUSDT", "4h")] = _sol(pair_pullback())
    _falling_btc(fake)
    engine, _ = _engine(api, service, store, strategy=_strategy(), dry_run=True)
    legacy = await engine.run_profile("conservador")
    assert legacy.regime is not None and legacy.regime["up"] is False
    assert (legacy.opened, legacy.family, legacy.regime_filter) == ((), "legacy", True)
    with capture_logs() as logs:
        local = await engine.run_profile("local")
    assert local.opened == ("SOLUSDT",)
    assert (local.family, local.regime_filter, local.setups) == ("pair_pullback_rsi", False, 1)
    summary = local.summary()
    assert (summary["family"], summary["regime_filter"], summary["filtered"]) == (
        "pair_pullback_rsi",
        False,
        [],
    )
    sol = next(e for e in logs if e["event"] == "decision.signal" and e["symbol"] == "SOLUSDT")
    assert (sol["setup"], sol["family"], sol["local_regime"]) == (
        "pair_pullback",
        "pair_pullback_rsi",
        "alta",
    )
    simulated = [e for e in await store.recent_events() if e.kind == "decision.entry_simulated"]
    assert [e.payload["symbol"] for e in simulated] == ["SOLUSDT"]


async def test_a_local_profile_goes_on_without_the_benchmark(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """The benchmark's candles fail: the legacy cycle fails with them, the local one only
    loses the regime telemetry (D-044, spec §2)."""
    _setup_market(fake)
    fake.candles[("SOLUSDT", "4h")] = _sol(pair_pullback())
    engine, _ = _engine(api, service, store, strategy=_strategy(), dry_run=True)
    await engine._universe.get()  # its daily candles come first: built before the faults
    fake.inject(Fault("GET", "/api/v3/klines", "reject", code=-1003, message="Too many requests."))
    with pytest.raises(BinanceError):
        await engine.run_profile("conservador")
    fake.inject(Fault("GET", "/api/v3/klines", "reject", code=-1003, message="Too many requests."))
    with capture_logs() as logs:
        report = await engine.run_profile("local")
    assert report.regime is None and report.opened == ("SOLUSDT",)
    (warning,) = [e for e in logs if e["event"] == "decision.benchmark_unavailable"]
    assert warning["profile"] == "ppb" and "Too many requests" in warning["error"]


async def test_a_stop_on_its_pair_holds_back_the_local_profiles_entry(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """The profile's budget (D-044) reaches the engine's pre-trade check: no entry on a pair
    for three candles after a stop on it."""
    _setup_market(fake)
    fake.candles[("SOLUSDT", "4h")] = _sol(pair_pullback())
    strategy = _strategy()
    profile = strategy.profiles["local"]
    assert profile.risk is not None
    limits = profile.risk.limits(strategy.profile_capital("local"), timedelta(hours=4))
    clock = Clock()
    engine, guard = _engine(
        api,
        service,
        store,
        strategy=strategy,
        dry_run=True,
        clock=clock,
        scopes={"local": ScopeSpec(profile.code, limits)},
    )
    await guard.track_scopes(snapshot(now=clock.now))  # the budget's first reading
    await closed_position(
        store,
        profile=profile.code,
        pnl="-1",
        closed_at=clock.now - timedelta(hours=1),
        symbol="SOLUSDT",
        exit_reason="stop_loss",
    )
    await guard.track_scopes(snapshot(now=clock.now))
    report = await engine.run_profile("local")
    assert report.opened == ()
    assert ("SOLUSDT", REENTRY) in report.rejected


async def test_local_signals_do_not_change_with_the_benchmark(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """BTC rising, falling or crashed: the local profile reads the same signals; the legacy
    one does not (its setups wait for the regime and its score has the relative strength)."""
    _setup_market(fake)
    fake.candles[("SOLUSDT", "4h")] = _sol(pair_pullback())
    strategy = _strategy()
    engine, _ = _engine(api, service, store, strategy=strategy, dry_run=True)
    universe = await engine._universe.get()
    now = api.rest.now_ms()
    readings: dict[str, Any] = {}
    for name, closes in {
        "up": [100 * math.exp(0.004 * i) for i in range(320)],
        "down": [100 * math.exp(-0.004 * i) for i in range(320)],
        "crash": [100.0] * 300 + [10.0] * 20,
    }.items():
        fake.candles[("BTCUSDT", "4h")] = _candles(closes)
        engine._candles = type(engine._candles)(api)  # no cached candles between readings
        local = strategy.profiles["local"]
        members = [m for m in universe.members if local.tier_limit(m.tier) > 0]
        readings[name] = {
            profile: (await engine._signals(members, strategy.profiles[profile], now))[0]
            for profile in ("local", "conservador")
        }
    # BTCUSDT is also a pair of the universe: its own signal follows its own candles
    pairs = {name: {"local": r["local"]["SOLUSDT"], "legacy": r["conservador"]["SOLUSDT"]}
             for name, r in readings.items()}  # fmt: skip
    assert pairs["up"]["local"] == pairs["down"]["local"] == pairs["crash"]["local"]
    assert pairs["up"]["local"].setup == "pair_pullback"
    assert pairs["up"]["legacy"].score != pairs["down"]["legacy"].score


async def _open(service: PositionService, store: Store, opened_at: datetime) -> int:
    position = await service.open_position(
        profile="ppb",
        entry=EntryOrder("SOLUSDT", D(1), D(150), EntryMode.LIMIT_FOK),
        policy=POLICY,
    )
    await store.update_position(position.id, opened_at=opened_at)
    return position.id


async def test_a_pullback_position_exits_on_its_technical_rule_outside_the_universe(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """Two closes below the EMA 20 with a low RSI close the position, even with its pair out
    of the profile's tiers: a held pair is always read."""
    _setup_market(fake)
    base = pair_pullback()
    falling = [*base["close"], *(base["close"].iloc[-1] * 0.96**k for k in range(1, 4))]
    fake.candles[("SOLUSDT", "4h")] = _sol(ohlcv(falling))
    clock = Clock()
    position_id = await _open(service, store, clock.now - timedelta(hours=8))
    strategy = _strategy(tiers={"core": 0.7, "large": 0.0, "mid": 0.7, "small": 0.0})
    engine, _ = _engine(api, service, store, strategy=strategy, clock=clock)
    report = await engine.run_profile("local")
    reason = TECHNICAL_EXITS[Family.PAIR_PULLBACK_RSI]
    assert report.exits == (("SOLUSDT", reason),)
    assert (await store.get_position(position_id)).state is PositionState.CLOSED
    assert await store.get_checkpoint(f"exit.weak.{position_id}") is None  # no rotation count


async def test_a_breakout_position_exits_below_the_level_its_entry_broke(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    base = pair_breakout()
    last = float(base["close"].iloc[-1])
    signal_index = len(base) - 1
    breakout_strategy = _strategy(
        family="pair_volatility_breakout",
        protection={
            "stop": {"atr_mult": 2.0, "max_pct": 7.0},
            "take_profit": {
                "mode": "trailing",
                "activation_r": 3.0,
                "trailing_atr_mult": 1.0,
            },
        },
    )
    opened_at = datetime.fromtimestamp((ALIGNED + (signal_index + 1) * FOUR_HOURS) / 1000 + 60, UTC)
    clock = Clock()
    clock.now = opened_at + timedelta(hours=8)  # within the maximum holding
    engine, _ = _engine(api, service, store, strategy=breakout_strategy, clock=clock)
    position_id = await _open(service, store, opened_at)
    # the entry candle closes above the level: no exit yet
    entry = ohlcv([*base["close"], last * 1.001], volumes=[*base["volume"], 100.0])
    fake.candles[("SOLUSDT", "4h")] = _sol(entry, ALIGNED)
    assert (await engine.run_profile("local")).exits == ()
    # the next one closes back below it
    failed = ohlcv([*entry["close"], last * 0.99], volumes=[*entry["volume"], 100.0])
    fake.candles[("SOLUSDT", "4h")] = _sol(failed, ALIGNED)
    engine._candles = type(engine._candles)(api)
    report = await engine.run_profile("local")
    assert report.exits == (("SOLUSDT", TECHNICAL_EXITS[Family.PAIR_VOLATILITY_BREAKOUT]),)
    assert (await store.get_position(position_id)).state is PositionState.CLOSED


async def test_a_position_without_its_signal_candle_has_no_level(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    fake.candles[("SOLUSDT", "4h")] = _sol(pair_breakout())
    breakout = _strategy(
        family="pair_volatility_breakout",
        protection={"take_profit": {"mode": "trailing", "trailing_atr_mult": 1.0}},
    )
    engine, _ = _engine(api, service, store, strategy=breakout)
    position_id = await _open(service, store, datetime(2020, 1, 1, tzinfo=UTC))
    position = await store.get_position(position_id)
    profile = breakout.profiles["local"]
    assert engine._entry_level(profile, position) is None
    assert engine._entry_level(profile, replace(position, opened_at=None)) is None


async def test_a_position_without_a_signal_keeps_only_the_holding_time(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """ETHUSDT has too few candles for the indicators: no technical exit to check."""
    _setup_market(fake)
    clock = Clock()
    position = await service.open_position(
        profile="ppb",
        entry=EntryOrder("ETHUSDT", D("0.01"), D(2500), EntryMode.LIMIT_FOK),
        policy=POLICY,
    )
    await store.update_position(position.id, opened_at=clock.now - timedelta(hours=8))
    engine, _ = _engine(api, service, store, strategy=_strategy(), clock=clock)
    report = await engine.run_profile("local")
    assert report.exits == ()
    assert (await store.get_position(position.id)).state is PositionState.PROTECTED
