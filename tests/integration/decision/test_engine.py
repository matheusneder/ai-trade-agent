"""Motor de decisão de ponta a ponta (Binance simulada + PostgreSQL)."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from tests.support.candles import HOUR_MS, ohlcv, raw_klines, uptrend_with_pullback
from tests.support.claude import FakeClaude, research_config
from tests.support.fake_binance import FakeBinance
from tests.support.metrics import Measured
from tests.support.risk import CONDITIONS, POLICY
from tests.support.tracing import Recorded
from trade_agent.decision.engine import DecisionEngine, UniverseCache
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.execution.orders import EntryMode, EntryOrder
from trade_agent.execution.positions import PositionState
from trade_agent.execution.service import PositionService
from trade_agent.market.universe import UniverseConfig
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.persistence.store import Severity, Store
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import WebResearchConfig
from trade_agent.research.service import ResearchService
from trade_agent.research.tagging import AssetTagger
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.state import GLOBAL, StateStore
from trade_agent.strategy.profiles import StrategyConfig

D = Decimal
FOUR_HOURS = 4 * HOUR_MS
FIXTURE = Path(__file__).parents[2] / "fixtures" / "profiles.yaml"
UNIVERSE = UniverseConfig(
    min_quote_volume_24h=D(1000), min_history_days=5, max_spread_bps=D(50), large_rank=5
)


def _strategy(**conservador: Any) -> StrategyConfig:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    profile = data["profiles"]["conservador"]
    profile["entry"]["min_score"] = 0.0
    profile["protection"]["take_profit"] = {
        "mode": "trailing", "activation_pct": 10.8, "trailing_delta_bps": 130,
    }  # fmt: skip
    profile.update(conservador)
    data["profiles"]["moderado"]["enabled"] = False
    return StrategyConfig.model_validate(data)


def _candles(closes: list[float]) -> list[list[Any]]:
    return raw_klines(ohlcv(closes, interval_ms=FOUR_HOURS))


def _setup_market(fake: FakeBinance, *, sol: list[float] | None = None) -> None:
    fake.prices.update({"ETHUSDT": D(2500), "SOLUSDT": D(150)})
    fake.volumes.update({"BTCUSDT": D(10**9), "ETHUSDT": D(10**8), "SOLUSDT": D(10**7)})
    fake.free["USDT"] = D(10_000)
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        fake.candles[(symbol, "1d")] = _candles([100.0] * 10)
    fake.candles[("BTCUSDT", "4h")] = _candles([100 * math.exp(0.004 * i) for i in range(320)])
    fake.candles[("ETHUSDT", "4h")] = _candles([100.0] * 100)  # histórico insuficiente
    closes = sol if sol is not None else uptrend_with_pullback()["close"].tolist()
    fake.candles[("SOLUSDT", "4h")] = _candles(closes)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 21, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _engine(
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    *,
    strategy: StrategyConfig | None = None,
    dry_run: bool = False,
    research: ResearchService | None = None,
    clock: Clock | None = None,
    alerts: list[str] | None = None,
) -> tuple[DecisionEngine, RiskGuard]:
    async def notify(severity: Severity, text: str) -> None:
        if alerts is not None:
            alerts.append(text)

    clock = clock or Clock()
    guard = RiskGuard(
        conditions=CONDITIONS, states=StateStore(store), store=store, notify=notify, clock=clock
    )
    engine = DecisionEngine(
        api=api,
        positions=service,
        store=store,
        guard=guard,
        strategy=strategy or _strategy(),
        universe=UniverseCache(api, UNIVERSE),
        research=research,
        dry_run=dry_run,
        notify=notify,
        clock=clock,
    )
    guard.set_flattener(engine.flatten)
    return engine, guard


async def test_dry_run_simulates_entries_without_orders(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    alerts: list[str] = []
    engine, _ = _engine(api, service, store, dry_run=True, alerts=alerts)
    with capture_logs() as logs:
        report = await engine.run_profile("conservador")
    assert report.opened == ("SOLUSDT",) and report.dry_run
    events = {e["event"]: e for e in logs}
    assert events["decision.cycle_start"]["eligible"] == 3
    sol = next(e for e in logs if e["event"] == "decision.signal" and e["symbol"] == "SOLUSDT")
    assert sol["setup"] == "trend_pullback" and sol["tier"] == "large"
    assert events["decision.reading"]["degraded"] is True
    assert [idea[0] for idea in events["decision.plan"]["ideas"]] == ["SOLUSDT"]
    assert report.evaluated == 2  # ETH sem histórico suficiente
    assert report.research == "sem analista"
    assert not fake.calls("POST", "/api/v3/orderList/opoco")
    assert await store.active_positions() == []
    kinds = [e.kind for e in await store.recent_events()]
    assert "decision.entry_simulated" in kinds and "decision.cycle" in kinds
    assert alerts == ["[simulação] conservador: entradas ['SOLUSDT']; saídas —"]


async def test_live_cycle_opens_protected_position_once(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    engine, _ = _engine(api, service, store)
    first = await engine.run_profile("conservador")
    assert first.opened == ("SOLUSDT",)
    (position,) = await store.active_positions()
    assert (position.symbol, position.profile, position.state) == (
        "SOLUSDT",
        "con",
        PositionState.PROTECTED,
    )
    second = await engine.run_profile("conservador")
    assert second.opened == () and ("SOLUSDT", "ativo já em carteira") in second.rejected
    assert len(fake.calls("POST", "/api/v3/orderList/opoco")) == 1


async def test_live_cycle_traces_decision_through_execution(
    spans: Recorded,
    measured: Measured,
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
) -> None:
    _setup_market(fake)
    engine, _ = _engine(api, service, store)
    await engine.run_profile("conservador")
    live = {"profile": "conservador", "dry_run": False}
    assert measured.value("trade_agent.decision.cycles", **live, state="running") == 1
    assert measured.value("trade_agent.decision.entries", **live) == 1
    assert "trade_agent.decision.exits" not in measured.points()  # nenhuma saída no ciclo
    cycle = spans.one("decision.cycle")
    attributes = cycle.attributes or {}
    assert attributes["trade_agent.profile"] == "conservador"
    assert tuple(attributes["trade_agent.opened"]) == ("SOLUSDT",)
    assert attributes["trade_agent.dry_run"] is False
    opened = spans.one("position.open")
    assert (opened.attributes or {})["trade_agent.symbol"] == "SOLUSDT"
    submit = spans.one("order_list.submit")
    assert spans.parent(submit) == opened
    assert spans.parent(spans.one("POST /api/v3/orderList/opoco")) == submit
    assert {
        ("decision", "exchange"),
        ("decision", "execution"),
        ("decision", "db"),
        ("execution", "exchange"),
        ("execution", "db"),
    } <= spans.edges()


async def test_pause_blocks_entries_and_pre_trade_rejects(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    engine, guard = _engine(api, service, store)
    await guard.pause(GLOBAL)
    paused = await engine.run_profile("conservador")
    assert paused.opened == () and paused.rejected == ()
    await guard.resume(GLOBAL)
    tight = _strategy(protection={
        "stop": {"mode": "fixed", "atr_mult": 2.0, "max_pct": 4},
        "take_profit": {"mode": "trailing", "activation_pct": 2, "trailing_delta_bps": 100},
    })  # fmt: skip
    engine, _ = _engine(api, service, store, strategy=tight)
    rejected = await engine.run_profile("conservador")
    assert rejected.opened == ()
    assert rejected.rejected[0][0] == "SOLUSDT" and "R:R" in rejected.rejected[0][1]


async def test_rule_exits_rotation_and_time(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    clock = Clock()
    engine, guard = _engine(api, service, store, clock=clock)
    await engine.run_profile("conservador")
    # o ativo passa a cair: score no nível de saída por 2 ciclos → rotação
    falling = [150 * math.exp(-0.004 * i) for i in range(320)]
    fake.candles[("SOLUSDT", "4h")] = _candles(falling)
    first = await engine.run_profile("conservador")
    assert first.exits == ()
    await guard.halt(GLOBAL)
    halted = await engine.run_profile("conservador")
    assert halted.exits == ()  # halt: sem saídas por regra
    await guard.resume(GLOBAL)
    second = await engine.run_profile("conservador")
    assert second.exits == (("SOLUSDT", "rotação: score no nível de saída"),)
    assert await store.active_positions() == []

    _setup_market(fake)
    await engine.run_profile("conservador")  # nova posição
    clock.now = datetime.now(UTC) + timedelta(days=22)  # opened_at usa o relógio real
    aged = await engine.run_profile("conservador")
    assert aged.exits == (("SOLUSDT", "tempo máximo de permanência"),)


async def test_dry_run_reports_exits_and_skips_unfilled_positions(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    live, _ = _engine(api, service, store)
    await live.run_profile("conservador")
    pending = EntryOrder("BTCUSDT", D("0.001"), D(50000), EntryMode.LIMIT_MAKER_GTC)
    await service.open_position(profile="con", entry=pending, policy=POLICY)  # sem execução
    clock = Clock()
    clock.now = datetime.now(UTC) + timedelta(days=30)
    alerts: list[str] = []
    dry, _ = _engine(api, service, store, dry_run=True, clock=clock, alerts=alerts)
    report = await dry.run_profile("conservador")
    assert report.exits == (("SOLUSDT", "tempo máximo de permanência"),)
    assert {p.symbol for p in await store.active_positions()} == {"SOLUSDT", "BTCUSDT"}
    assert alerts[0].startswith("[simulação] conservador: entradas —")


async def test_break_even_adjusts_protection(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    engine, _ = _engine(api, service, store)
    await engine.run_profile("conservador")
    fake.set_price("SOLUSDT", D(160))  # ganho acima de 1R: stop sobe para o break-even
    await engine.run_profile("conservador")
    (position,) = await store.active_positions()
    assert position.protection_seq == 1
    assert position.state is PositionState.PROTECTED


async def test_analyst_veto_blocks_entry_and_exits_holding(
    db: Database, api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    claude = FakeClaude()
    view = {
        "market_regime": "neutral", "global_sentiment": 0, "exposure_multiplier": 1,
        "global_risk_flags": [], "assets": [],
    }  # fmt: skip
    veto = {**view, "assets": [{
        "asset": "SOL", "sentiment": -0.9, "confidence": 0.9, "horizon": "days",
        "catalysts": [], "risk_flags": ["exploit"], "veto": True, "rationale": "r",
        "sources": [],
    }]}  # fmt: skip
    config = research_config(web=WebResearchConfig(enabled=False))
    async with httpx.AsyncClient() as http:
        research = ResearchService(
            store=ResearchStore(db),
            collector=NewsCollector(http, config.sources, AssetTagger([], {})),
            client=claude.client(),
            config=config,
            clock=Clock(),
        )
        engine, _ = _engine(api, service, store, research=research)
        claude.reply_json(veto)
        vetoed = await engine.run_profile("conservador")
        assert vetoed.opened == () and vetoed.research == "ok"
        assert ("SOLUSDT", "veto do analista") in vetoed.rejected

        claude.reply_json(view)
        opened = await engine.run_profile("conservador")
        assert opened.opened == ("SOLUSDT",)

        claude.fail(500)  # ciclo falha: usa a última leitura válida (sem veto)
        failed = await engine.run_profile("conservador")
        assert failed.research.startswith("falhou") and failed.exits == ()

        claude.reply_json(veto)
        exit_report = await engine.run_profile("conservador")
        assert exit_report.exits == (("SOLUSDT", "veto do analista"),)

        fake.candles[("SOLUSDT", "4h")] = _candles([150.0] * 320)  # sem setup nem posição
        quiet = await engine.run_profile("conservador")
        assert quiet.research == "sem candidatos (leitura anterior)"
    assert len(claude.requests) == 4


async def test_flatten_sells_filled_and_cancels_pending(
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_market(fake)
    engine, guard = _engine(api, service, store)
    await engine.run_profile("conservador")
    pending = EntryOrder("BTCUSDT", D("0.001"), D(50000), EntryMode.LIMIT_MAKER_GTC)
    await service.open_position(profile="mod", entry=pending, policy=POLICY)
    assert await engine.flatten("moderado") == 1  # só o escopo pedido
    states = {p.symbol: p.state for p in await store.active_positions()}
    assert states == {"SOLUSDT": PositionState.PROTECTED}

    closed = await guard.flatten(GLOBAL)
    assert closed == 1 and await store.active_positions() == []

    await engine.run_profile("conservador")  # halt: nenhuma entrada nova
    assert await store.active_positions() == []
    await guard.resume(GLOBAL)
    await engine.run_profile("conservador")

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("falha no encerramento")

    monkeypatch.setattr(service, "close_position", broken)
    assert await engine.flatten(GLOBAL) == 0
    assert "flatten.failed" in [e.kind for e in await store.recent_events()]


async def test_empty_universe_is_reported(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    fake.volumes.clear()  # como no Spot Testnet: nada passa no filtro de volume
    engine, _ = _engine(api, service, store, dry_run=True)
    with capture_logs() as logs:
        report = await engine.run_profile("conservador")
    assert report.evaluated == 0 and report.opened == ()
    (warning,) = [e for e in logs if e["event"] == "decision.empty_universe"]
    assert warning["excluded"] == {"volume insuficiente": 3}


async def test_universe_cache_ttl(api: BinanceSpotApi, fake: FakeBinance) -> None:
    _setup_market(fake)
    now = [0.0]
    cache = UniverseCache(api, UNIVERSE, ttl=timedelta(minutes=1), clock=lambda: now[0])
    first = await cache.get()
    assert [m.symbol for m in first.members] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    calls = len(fake.calls("GET", "/api/v3/exchangeInfo"))
    assert await cache.get() is first
    now[0] = 61
    assert await cache.get() is not first
    assert len(fake.calls("GET", "/api/v3/exchangeInfo")) == calls + 1
