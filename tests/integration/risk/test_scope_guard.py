"""The pair-local profiles' risk budget (D-044) in the production Risk Guard, with PostgreSQL.

``RiskGuard.track_scopes`` runs the lab's code (``trade_agent.risk.scope``) on each profile's
own state: the same readings give the same pauses, and a pause shows as the profile's state.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.support.risk import CONDITIONS, NOW, closed_position, snapshot
from trade_agent.app import scope_specs
from trade_agent.persistence.store import Severity, Store
from trade_agent.risk.guard import RiskGuard, RiskSnapshot, ScopeSpec
from trade_agent.risk.scope import (
    DAILY_LOSS,
    DRAWDOWN,
    LOSING_STREAK,
    REENTRY,
    ScopeLimits,
    ScopeRisk,
)
from trade_agent.risk.state import GLOBAL, OpState, StateStore
from trade_agent.strategy.profiles import load_strategy_config

D = Decimal
H = timedelta(hours=1)
LIMITS = ScopeLimits(
    capital=100.0,
    daily_loss_pct=1.5,
    consecutive_losses=3,
    cooldown=24 * H,
    max_drawdown_pct=8.0,
    reentry=3 * H,
)
CODE = "ppb"
REPOSITORY = Path(__file__).parents[3] / "config" / "profiles.yaml"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _guard(
    store: Store,
    clock: Clock,
    alerts: list[tuple[Severity, str]],
    limits: ScopeLimits = LIMITS,
    scopes: dict[str, ScopeSpec] | None = None,
) -> RiskGuard:
    async def notify(severity: Severity, text: str) -> None:
        alerts.append((severity, text))

    return RiskGuard(
        conditions=CONDITIONS,
        states=StateStore(store),
        store=store,
        notify=notify,
        clock=clock,
        scopes={"local": ScopeSpec(CODE, limits)} if scopes is None else scopes,
    )


def _reading(at: datetime, pnl: str) -> RiskSnapshot:
    """The profile's result since its start, realized and open, at ``at``."""
    return snapshot(now=at, profile_pnl={"local": D(pnl)})


async def _track(guard: RiskGuard, clock: Clock, at: datetime, pnl: str) -> None:
    clock.now = at
    await guard.track_scopes(_reading(at, pnl))


async def test_before_its_first_reading_the_budget_blocks_nothing(store: Store) -> None:
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, Clock(NOW), alerts)
    scope = await guard.scope_risk("local")
    assert scope is not None and scope.blocked(NOW, "SOLUSDT") is None
    assert await guard.resume("local")  # nothing to review yet
    assert await store.get_checkpoint("risk.scope.local") is None


async def test_the_daily_loss_pauses_the_profile_for_its_cooldown(store: Store) -> None:
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, clock, alerts)
    await _track(guard, clock, NOW, "0")
    await _track(guard, clock, NOW + H, "-1.49")
    assert (await guard.state("local")).state is OpState.RUNNING
    await _track(guard, clock, NOW + 2 * H, "-1.5")  # 1.5% of its capital, marked to market
    state = await guard.state("local")
    assert (state.state, state.reason, state.until) == (OpState.PAUSED, DAILY_LOSS, NOW + 26 * H)
    assert alerts == [
        (Severity.CRITICAL, f"[local] PAUSED até 2026-09-27 14:00 UTC — {DAILY_LOSS}")
    ]
    await _track(guard, clock, NOW + 3 * H, "-4")  # the same day: it acts once
    assert len(alerts) == 1
    assert (await guard.state(GLOBAL)).state is OpState.RUNNING
    clock.now = NOW + 26 * H
    assert (await guard.state("local")).state is OpState.RUNNING  # the cooldown expired
    scope = await guard.scope_risk("local")
    assert scope is not None and scope.blocked(NOW + 26 * H, "SOLUSDT") is None


async def test_a_losing_streak_counts_the_closed_trades_after_the_first_reading(
    store: Store,
) -> None:
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, clock, alerts)
    await closed_position(store, profile=CODE, pnl="-1", closed_at=NOW - H)  # before the budget
    await _track(guard, clock, NOW, "-1")
    for k in (1, 2):
        await closed_position(store, profile=CODE, pnl="-0.2", closed_at=NOW + k * H)
    await closed_position(store, profile="con", pnl="-5", closed_at=NOW + 2 * H)  # another profile
    await _track(guard, clock, NOW + 3 * H, "-1.4")
    assert (await guard.state("local")).state is OpState.RUNNING
    third = NOW + 3 * H + timedelta(minutes=30)
    await closed_position(store, profile=CODE, pnl="-0.2", closed_at=third)
    await _track(guard, clock, NOW + 4 * H, "-1.6")
    state = await guard.state("local")
    assert (state.state, state.reason) == (OpState.PAUSED, LOSING_STREAK)
    assert state.until == third + 24 * H


async def test_the_drawdown_pauses_new_entries_until_the_review(store: Store) -> None:
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, clock, alerts, replace(LIMITS, daily_loss_pct=None))
    await _track(guard, clock, NOW, "0")
    await _track(guard, clock, NOW + H, "10")  # the peak: 110
    await _track(guard, clock, NOW + 2 * H, "1.3")  # -7.9%
    assert (await guard.state("local")).state is OpState.RUNNING
    await _track(guard, clock, NOW + 3 * H, "1")  # -8.2%
    state = await guard.state("local")
    assert (state.state, state.reason, state.until) == (OpState.PAUSED, DRAWDOWN, None)
    assert state.state.allows_rule_exits  # not HALTED: the technical exits go on
    await _track(guard, clock, NOW + 1000 * H, "30")  # no expiry, not even on a recovery
    assert (await guard.state("local")).state is OpState.PAUSED
    scope = await guard.scope_risk("local")
    assert scope is not None and scope.blocked(clock.now, "SOLUSDT") == DRAWDOWN

    assert await guard.resume("local")  # the review
    assert (await guard.state("local")).state is OpState.RUNNING
    scope = await guard.scope_risk("local")
    assert scope is not None and scope.blocked(clock.now, "SOLUSDT") is None
    await _track(guard, clock, NOW + 1001 * H, "30")  # the drawdown counts from here
    await _track(guard, clock, NOW + 1002 * H, "20.5")  # -7.3%
    assert (await guard.state("local")).state is OpState.RUNNING
    assert [text for _, text in alerts] == [
        f"[local] PAUSED — {DRAWDOWN}",
        "[local] RUNNING — retomado pelo operador",
    ]


async def test_a_stop_holds_back_an_entry_on_its_pair_for_three_candles(store: Store) -> None:
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, clock, alerts)
    await _track(guard, clock, NOW, "0")
    closes = (("SOLUSDT", "-0.5", "stop_loss"), ("ETHUSDT", "0.8", "take_profit"))
    for symbol, pnl, reason in closes:
        await closed_position(
            store, profile=CODE, pnl=pnl, closed_at=NOW + H, symbol=symbol, exit_reason=reason
        )
    await _track(guard, clock, NOW + 2 * H, "0.3")
    scope = await guard.scope_risk("local")
    assert scope is not None
    assert scope.blocked(NOW + 2 * H, "SOLUSDT") == REENTRY
    assert scope.blocked(NOW + 2 * H, "ETHUSDT") is None
    assert scope.blocked(NOW + 4 * H, "SOLUSDT") is None
    assert (await guard.state("local")).state is OpState.RUNNING  # a wait, not a pause
    assert alerts == []


async def test_a_capital_change_is_no_result(store: Store) -> None:
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    await _track(_guard(store, clock, alerts, replace(LIMITS, capital=200.0)), clock, NOW, "0")
    smaller = _guard(store, clock, alerts)  # the managed capital went from 200 to 100 USDT
    await _track(smaller, clock, NOW + H, "0")
    assert (await smaller.state("local")).state is OpState.RUNNING
    scope = await smaller.scope_risk("local")
    assert scope is not None and (scope.peak, scope.day_start) == (100.0, 100.0)
    assert alerts == []


async def test_the_production_budget_decides_as_the_labs(store: Store) -> None:
    """Parity: the same closed trades and marks, through the database and the guard, and
    straight into the lab's ``ScopeRisk``, block the same entries at every reading."""
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    guard = _guard(store, clock, alerts)
    lab = ScopeRisk(LIMITS)
    # (reading, profile result, trades closed since the previous reading: offset, pnl, symbol,
    # closed by the stop)
    script: list[tuple[timedelta, str, list[tuple[timedelta, str, str, bool]]]] = [
        (0 * H, "0", []),
        (2 * H, "-0.4", [(1 * H, "-0.4", "SOLUSDT", True)]),
        (4 * H, "-0.9", [(3 * H, "-0.5", "ETHUSDT", False)]),
        (6 * H, "-1.1", [(5 * H, "-0.2", "SOLUSDT", True)]),  # the streak: paused 24h
        (20 * H, "2", []),  # a new day
        (30 * H, "6", [(29 * H, "3.1", "ETHUSDT", False)]),  # the peak: 106
        (34 * H, "0.4", []),  # -1.6 since the day's first mark: the daily loss
        (60 * H, "-3", [(50 * H, "-7.4", "LINKUSDT", True)]),  # -8.5% from the peak: review
        (62 * H, "-2", []),
    ]
    symbols = ("SOLUSDT", "ETHUSDT", "LINKUSDT")
    for reading, pnl, trades in script:
        for offset, result, symbol, stop in trades:
            await closed_position(
                store,
                profile=CODE,
                pnl=result,
                closed_at=NOW + offset,
                symbol=symbol,
                exit_reason="stop_loss" if stop else "decision",
            )
            lab.closed(NOW + offset, symbol, float(result), stop=stop)
        lab.mark(NOW + reading, LIMITS.capital + float(pnl))
        await _track(guard, clock, NOW + reading, pnl)
        production = await guard.scope_risk("local")
        assert production is not None
        for at in (NOW + reading, NOW + reading + 2 * H):
            for symbol in symbols:
                assert production.blocked(at, symbol) == lab.blocked(at, symbol), (reading, symbol)
        assert production.to_json() == lab.to_json()
        state = await guard.state("local")
        assert state.state.allows_entries == (lab.blocked(NOW + reading, "BTCUSDT") is None)
    assert [reason for _, reason in lab.events] == [LOSING_STREAK, DAILY_LOSS, DRAWDOWN]
    assert [text.split(" — ")[1] for _, text in alerts] == [LOSING_STREAK, DAILY_LOSS, DRAWDOWN]


async def test_without_a_local_profile_the_guard_is_as_before(store: Store) -> None:
    """Regression: without the pair-local ones (enabled since D-046), the repository's profiles
    give the guard no budget, so ``track_scopes`` reads and writes nothing and no state
    changes, whatever the profiles' results."""
    clock = Clock(NOW)
    alerts: list[tuple[Severity, str]] = []
    config = load_strategy_config(REPOSITORY)
    legacy = {name: p for name, p in config.profiles.items() if not p.family.local}
    specs = scope_specs(config.model_copy(update={"profiles": legacy}))
    assert specs == {}
    guard = _guard(store, clock, alerts, scopes=specs)
    names = ("swing_trend", "momentum_alpha", "alt_atr")
    await closed_position(store, profile="swt", pnl="-50", closed_at=NOW, exit_reason="stop_loss")
    reading = snapshot(profile_pnl={name: D(-500) for name in names}, equity=D(500))
    await guard.track_scopes(reading)
    assert alerts == []
    for name in (GLOBAL, *names):
        assert (await guard.state(name)).state is OpState.RUNNING
        assert await guard.scope_risk(name) is None
        assert await store.get_checkpoint(f"risk.scope.{name}") is None
