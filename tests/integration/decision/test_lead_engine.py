"""The decision engine with a ``lead_consensus`` profile (D-052): it buys only what its lead
traders bought and its own candles confirm, with the profile's own size and protection, and
without a fresh reading of the traders it buys nothing."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
from structlog.testing import capture_logs

from tests.integration.decision.test_engine import (
    FOUR_HOURS,
    Clock,
    _engine,
    _setup_market,
    _strategy_data,
)
from tests.support.candles import downtrend, ohlcv, pair_pullback, raw_klines
from tests.support.fake_binance import FakeBinance
from tests.support.leads import FakeSource, active, stats, trade
from tests.support.local_profiles import LOCAL_PULLBACK, merge
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.execution.positions import PositionState
from trade_agent.execution.service import PositionService
from trade_agent.leads.consensus import NO_TRACKER, SELL_CONSENSUS, STALE_RANKING
from trade_agent.leads.source import LeadTrade
from trade_agent.leads.tracker import LeadTracker
from trade_agent.persistence.store import Store
from trade_agent.signals import Family
from trade_agent.signals.local import DOWNTREND, LEAD_CONSENSUS, TECHNICAL_EXITS
from trade_agent.strategy.profiles import StrategyConfig

D = Decimal
LEAD: dict[str, Any] = {
    "code": "lead",
    "enabled": True,
    "family": "lead_consensus",
    "timeframe": "4h",
    "tiers": {"core": 0.7, "large": 0.7, "mid": 0.7, "small": 0.0},
    "protection": {
        "stop": {"mode": "fixed", "atr_mult": 2.0, "max_pct": 6.0},
        "take_profit": {"mode": "limit", "activation_pct": 0.5, "activation_r": 2.0},
        "max_holding": "72h",
    },
    "leads": {},
}


def _strategy(**lead: Any) -> StrategyConfig:
    data = _strategy_data()
    data["profiles"]["lead"] = merge(LOCAL_PULLBACK, merge(LEAD, lead))
    return StrategyConfig.model_validate(data)


def _sol(frame: pd.DataFrame) -> list[list[Any]]:
    return raw_klines(
        ohlcv(frame["close"].tolist(), volumes=frame["volume"].tolist(), interval_ms=FOUR_HOURS)
    )


def _market(fake: FakeBinance, frame: pd.DataFrame | None = None) -> None:
    """SOL's own candles confirm a purchase (above its EMA 20, not stretched), at 150 USDT."""
    _setup_market(fake)
    fake.candles[("SOLUSDT", "4h")] = _sol(pair_pullback() if frame is None else frame)


async def _tracker(
    store: Store, clock: Clock, **trades: list[LeadTrade]
) -> tuple[LeadTracker, FakeSource]:
    """A tracker that has just read its lead traders ``ana``, ``bia``..., each with its trades."""
    source = FakeSource([stats(lead) for lead in trades], trades=trades)

    async def no_wait(_: float) -> None:
        return None

    tracker = LeadTracker(
        profile="lead",
        config=_strategy().profiles["lead"].leads,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        store=store,
        quote_asset="USDT",
        clock=clock,
        sleep=no_wait,
    )
    await tracker.refresh()
    return tracker, source


def _bought(clock: Clock, symbol: str = "SOLUSDT", price: str = "150") -> list[LeadTrade]:
    return active(trade(symbol, quote="300", price=price, now=clock.now), now=clock.now)


def _sold(clock: Clock, symbol: str = "SOLUSDT") -> list[LeadTrade]:
    return active(trade(symbol, buy=False, quote="300", now=clock.now), now=clock.now)


async def test_a_confirmed_consensus_opens_a_position_with_the_profiles_own_protection(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    tracker, _ = await _tracker(store, clock, ana=_bought(clock), bia=_bought(clock))
    engine, _ = _engine(
        api, service, store, strategy=_strategy(), clock=clock, leads={"lead": tracker}
    )
    with capture_logs() as logs:
        report = await engine.run_profile("lead")
    assert report.opened == ("SOLUSDT",)
    assert (report.family, report.regime_filter, report.setups) == ("lead_consensus", False, 1)
    # who was behind it goes to the cycle's event: the entry can be traced to the traders
    assert report.leads is not None
    assert (report.leads["status"], report.leads["selected"], report.leads["fresh"]) == ("ok", 2, 2)
    assert report.leads["assets"] == {
        "SOLUSDT": {
            "buyers": ["ana", "bia"],
            "sellers": [],
            "avg_buy_price": "150",
            "consensus": "buy",
        }
    }
    (event,) = [e for e in await store.recent_events() if e.kind == "decision.cycle"]
    assert event.payload["leads"] == report.leads
    sol = next(e for e in logs if e["event"] == "decision.signal" and e["symbol"] == "SOLUSDT")
    assert (sol["setup"], sol["score"]) == (LEAD_CONSENSUS, 1.0)  # both followed traders
    # nothing of the traders' trade is copied: the size is the profile's risk, the stop the
    # pair's own structure, the target 2R net, all on Binance from the first moment
    (position,) = await store.active_positions()
    assert (position.profile, position.symbol) == ("lead", "SOLUSDT")
    assert position.state is PositionState.PROTECTED
    stop = position.policy.stop_pct
    assert stop is not None and D(2) < stop <= D(6)
    assert position.policy.take_profit_pct == 2 * (stop + D("0.3")) + D("0.3")
    cost = position.entry_quote
    assert cost is not None and cost * (stop + D("0.3")) / 100 <= D("0.5") * D("1.05")


async def test_one_trader_alone_or_no_trader_at_all_opens_nothing(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    tracker, _ = await _tracker(store, clock, ana=_bought(clock), bia=active(now=clock.now))
    engine, _ = _engine(
        api,
        service,
        store,
        strategy=_strategy(),
        clock=clock,
        dry_run=True,
        leads={"lead": tracker},
    )
    report = await engine.run_profile("lead")
    assert (report.opened, report.setups, report.filtered) == ((), 0, ())
    assert report.leads is not None and report.leads["status"] == "ok"
    assert report.leads["assets"]["SOLUSDT"]["consensus"] is None  # type: ignore[index]
    assert report.rejected == ()


async def test_the_candles_can_refuse_what_the_traders_bought(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake, downtrend())
    clock = Clock()
    tracker, _ = await _tracker(store, clock, ana=_bought(clock), bia=_bought(clock))
    engine, _ = _engine(
        api,
        service,
        store,
        strategy=_strategy(),
        clock=clock,
        dry_run=True,
        leads={"lead": tracker},
    )
    report = await engine.run_profile("lead")
    assert report.opened == () and report.setups == 0
    assert report.filtered == (("SOLUSDT", DOWNTREND),)  # what the agent declined to follow
    assert report.leads is not None
    assert report.leads["assets"]["SOLUSDT"]["consensus"] == "buy"  # type: ignore[index]


async def test_a_price_that_ran_away_from_what_the_traders_paid_is_not_chased(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    cheap = _bought(clock, price="140")  # the ask is 150: 7% above what they paid
    tracker, _ = await _tracker(store, clock, ana=cheap, bia=cheap)
    engine, _ = _engine(
        api,
        service,
        store,
        strategy=_strategy(),
        clock=clock,
        dry_run=True,
        leads={"lead": tracker},
    )
    report = await engine.run_profile("lead")
    assert report.opened == () and report.setups == 1
    assert report.rejected == (("SOLUSDT", "preço já avançou além do limite do sinal"),)


async def test_without_a_fresh_reading_of_the_traders_nothing_is_bought(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    tracker, source = await _tracker(store, clock, ana=_bought(clock), bia=_bought(clock))
    engine, _ = _engine(
        api,
        service,
        store,
        strategy=_strategy(),
        clock=clock,
        dry_run=True,
        leads={"lead": tracker},
    )
    # the source goes down: after max_ranking_age the traders are no longer followed
    source.fail("ranking")
    source.fail("trades")
    clock.now += timedelta(hours=28)
    await tracker.refresh()
    report = await engine.run_profile("lead")
    assert (report.opened, report.setups) == ((), 0)
    assert report.leads == {
        "status": STALE_RANKING,
        "selected": 0,
        "fresh": 0,
        "as_of": None,
        "assets": {},
    }
    # and a profile wired without its tracker has no consensus either
    bare, _ = _engine(api, service, store, strategy=_strategy(), clock=clock, dry_run=True)
    orphan = await bare.run_profile("lead")
    assert orphan.opened == () and orphan.leads is not None
    assert orphan.leads["status"] == NO_TRACKER


async def test_a_sell_consensus_closes_the_position_and_names_itself(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    tracker, source = await _tracker(store, clock, ana=_bought(clock), bia=_bought(clock))
    engine, _ = _engine(
        api, service, store, strategy=_strategy(), clock=clock, leads={"lead": tracker}
    )
    assert (await engine.run_profile("lead")).opened == ("SOLUSDT",)
    # four hours later both sold more than they had bought
    clock.now += timedelta(hours=4)
    source.history = {"ana": _sold(clock), "bia": _sold(clock)}
    await tracker.refresh()
    report = await engine.run_profile("lead")
    assert report.exits == (("SOLUSDT", SELL_CONSENSUS),)
    assert report.opened == ()
    assert await store.active_positions() == []


async def test_the_pairs_own_downtrend_closes_the_position_whatever_the_traders_do(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    tracker, _ = await _tracker(store, clock, ana=_bought(clock), bia=_bought(clock))
    engine, _ = _engine(
        api, service, store, strategy=_strategy(), clock=clock, leads={"lead": tracker}
    )
    assert (await engine.run_profile("lead")).opened == ("SOLUSDT",)
    fake.candles[("SOLUSDT", "4h")] = _sol(downtrend())
    engine._candles = type(engine._candles)(api)
    report = await engine.run_profile("lead")  # the traders still hold their purchase
    assert report.exits == (("SOLUSDT", TECHNICAL_EXITS[Family.LEAD_CONSENSUS]),)
    assert await store.active_positions() == []


async def test_the_other_profiles_never_read_the_lead_traders(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _market(fake)
    clock = Clock()
    tracker, _ = await _tracker(store, clock, ana=_bought(clock), bia=_bought(clock))
    engine, _ = _engine(
        api,
        service,
        store,
        strategy=_strategy(),
        clock=clock,
        dry_run=True,
        leads={"lead": tracker},
    )
    legacy = await engine.run_profile("conservador")
    assert legacy.leads is None and legacy.summary()["leads"] is None
    assert legacy.family == "legacy"
