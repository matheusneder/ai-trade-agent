"""Risk Guard e monitor com PostgreSQL e Binance simulada (critério de saída da Fase 5)."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.support.fake_binance import FakeBinance
from tests.support.risk import CONDITIONS, NOW, POLICY, TRIGGERS, closed_position, snapshot
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.rest import CallHealth
from trade_agent.execution.orders import EntryOrder
from trade_agent.execution.service import PositionService
from trade_agent.persistence.store import Severity, Store
from trade_agent.reconcile.reconciler import ReconcileReport
from trade_agent.risk.conditions import Action
from trade_agent.risk.guard import RiskGuard, evaluate
from trade_agent.risk.monitor import EQUITY_KEY, RiskMonitor, quote_deviation
from trade_agent.risk.state import GLOBAL, OpState, ScopeState, StateStore
from trade_agent.strategy.profiles import load_strategy_config

D = Decimal
PROFILES = load_strategy_config(Path(__file__).parents[2] / "fixtures" / "profiles.yaml")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _guard(store: Store, clock: Clock, alerts: list[tuple[Severity, str]]) -> RiskGuard:
    async def notify(severity: Severity, text: str) -> None:
        alerts.append((severity, text))

    return RiskGuard(
        conditions=CONDITIONS, states=StateStore(store), store=store, notify=notify, clock=clock
    )


@pytest.mark.parametrize(("condition", "scope", "overrides", "action"), TRIGGERS)
async def test_each_forced_condition_applies_its_action(
    store: Store, condition: str, scope: str, overrides: dict[str, Any], action: Action
) -> None:
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, Clock(NOW), alerts)
    flattened: list[str] = []

    async def flatten(target: str) -> int:
        flattened.append(target)
        return 2

    guard.set_flattener(flatten)
    hits = evaluate(CONDITIONS, snapshot(**overrides))
    assert [h.condition for h in await guard.apply(hits)] == [condition]

    state = await guard.state(scope)
    if action is Action.PAUSE:
        assert state.state is OpState.PAUSED
        cooldown = hits[0].cooldown
        assert state.until == (NOW + cooldown if cooldown else None)
    else:
        assert state.state is OpState.HALTED
    assert (flattened == [scope]) is (action is Action.FLATTEN)
    if action is Action.FLATTEN:
        assert "flatten: 2 posições" in (state.reason or "")
    assert condition in (state.reason or "")
    assert not (await guard.effective("moderado")).allows_entries
    assert alerts and alerts[0][0] is Severity.CRITICAL
    assert "risk.state_changed" in [e.kind for e in await store.recent_events()]
    assert await guard.apply(hits) == []  # repetir o gatilho não muda nada


async def test_pause_expires_extends_and_never_relaxes(store: Store) -> None:
    clock = Clock(NOW)
    guard = _guard(store, clock, [])
    btc_drop = evaluate(CONDITIONS, snapshot(btc_change_1h=-0.07))
    fear = evaluate(CONDITIONS, snapshot(fear_greed=5))
    await guard.apply(btc_drop)  # pausa de 4h
    assert (await guard.state(GLOBAL)).until == NOW + timedelta(hours=4)
    await guard.apply(fear)  # 24h: estende
    assert (await guard.state(GLOBAL)).until == NOW + timedelta(hours=24)
    await guard.apply(btc_drop)  # mais curta: ignorada
    assert (await guard.state(GLOBAL)).until == NOW + timedelta(hours=24)
    clock.now = NOW + timedelta(hours=24)
    assert (await guard.effective("conservador")) is OpState.RUNNING  # cooldown vencido
    await guard.apply(evaluate(CONDITIONS, snapshot(equity=D(800), peak_equity=D(1000))))
    await guard.apply(btc_drop)  # halt nunca vira pausa
    assert (await guard.state(GLOBAL)).state is OpState.HALTED


async def test_manual_commands(store: Store) -> None:
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, Clock(NOW), alerts)
    with pytest.raises(RuntimeError, match="executor"):
        await guard.flatten("conservador")
    await StateStore(store).put("moderado", ScopeState(OpState.FLATTENING, "em andamento"))
    assert not await guard.resume("moderado")
    await guard.pause("conservador")
    assert (await guard.effective("conservador")) is OpState.PAUSED
    assert (await guard.effective("moderado")) is OpState.FLATTENING
    await guard.halt(GLOBAL, "teste")
    assert (await guard.effective("conservador")) is OpState.HALTED
    assert await guard.resume(GLOBAL)

    async def flatten(scope: str) -> int:
        return 0

    guard.set_flattener(flatten)
    assert await guard.flatten("conservador") == 0
    assert (await guard.state("conservador")).state is OpState.HALTED
    assert await guard.resume("conservador")
    assert (await guard.effective("conservador")) is OpState.RUNNING
    assert alerts[-1] == (Severity.INFO, "[conservador] RUNNING — retomado pelo operador")


# ============================================================================ monitor
def _klines(closes: list[float]) -> list[list[Any]]:
    return [[i * 60_000, "0", "0", "0", str(c), "1", i * 60_000 + 59_999, "1", 1, "0", "0", "0"]
            for i, c in enumerate(closes)]  # fmt: skip


async def test_monitor_snapshot(
    store: Store, api: BinanceSpotApi, fake: FakeBinance, service: PositionService
) -> None:
    fake.prices.update({"USDCUSDT": D("1.02"), "FDUSDUSDT": D("1.04")})
    fake.candles[("BTCUSDT", "1m")] = _klines([60000.0] + [60500.0] * 59 + [57000.0])
    midnight = NOW.replace(hour=0)
    await closed_position(store, profile="con", pnl="5", closed_at=midnight - timedelta(hours=2))
    await closed_position(store, profile="mod", pnl="-2", closed_at=midnight + timedelta(hours=1))
    await closed_position(store, profile="con", pnl="-3", closed_at=midnight + timedelta(hours=2))
    await closed_position(store, profile="con", pnl="-1", closed_at=midnight + timedelta(hours=3))
    opened = await service.open_position(
        profile="con", entry=EntryOrder("BTCUSDT", D("0.01"), D(63100)), policy=POLICY
    )
    fake.set_price("BTCUSDT", D(62000))
    assert opened.entry_price is not None and opened.protected_qty is not None
    unrealized = (D(62000) - opened.entry_price) * opened.protected_qty
    health = CallHealth()
    for ok in (False, True, True, True, True):
        health.record(ok=ok)
    monitor = RiskMonitor(
        api=api, store=store, strategy=PROFILES, health=health, fear_greed=lambda: 22,
        clock=lambda: NOW,
    )  # fmt: skip
    monitor.note_reconcile(ReconcileReport(started_at=NOW, orphans=["x"], errors=["e"]))
    with capture_logs() as logs:
        first = await monitor.snapshot()
    (reading,) = [e for e in logs if e["event"] == "risk.snapshot"]
    assert reading["positions"] == 1 and reading["fear_greed"] == 22
    assert reading["equity"] == str(first.equity)
    assert first.equity == D(1000) + D(-1) + unrealized  # capital + realizado + aberto
    assert first.day_start_equity == first.peak_equity == first.equity
    assert first.consecutive_losses == 3
    assert first.profile_consecutive_losses == {"conservador": 2, "moderado": 1, "agressivo": 0}
    assert first.profile_daily_pnl == {"conservador": D(-4), "moderado": D(-2), "agressivo": 0}
    assert first.btc_change_1h == pytest.approx(-0.05)
    assert first.quote_deviation == pytest.approx(abs(1 / 1.03 - 1))  # mediana 1,03
    assert (first.fear_greed, first.api_error_rate, first.reconcile_anomalies) == (22, 0.2, 2)

    fake.set_price("BTCUSDT", D(64000))  # patrimônio sobe: novo pico, mesma abertura do dia
    second = await monitor.snapshot()
    assert second.peak_equity == second.equity > first.equity
    assert second.day_start_equity == first.equity
    next_day = RiskMonitor(
        api=api, store=store, strategy=PROFILES, health=health,
        clock=lambda: NOW + timedelta(days=1),
    )  # fmt: skip
    third = await next_day.snapshot()
    assert third.day_start_equity == third.equity and third.fear_greed is None
    saved = await store.get_checkpoint(EQUITY_KEY)
    assert saved is not None and saved["day"] == "2026-09-27"


async def test_monitor_without_market_references(
    store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    fake.candles[("BTCUSDT", "1m")] = _klines([60000.0] * 10)  # menos de 61 candles
    monitor = RiskMonitor(api=api, store=store, strategy=PROFILES, health=CallHealth())
    reading = await monitor.snapshot()
    assert reading.btc_change_1h is None and reading.quote_deviation is None
    fake.candles[("BTCUSDT", "1m")] = _klines([0.0] * 61)
    assert (await monitor.snapshot()).btc_change_1h is None
    assert quote_deviation({}) is None
