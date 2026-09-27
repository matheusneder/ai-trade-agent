from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.support.exchange_info import rules_for
from tests.support.risk import CONDITIONS, TRIGGERS, snapshot
from trade_agent.execution.orders import EntryOrder, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.market.universe import Tier
from trade_agent.risk.conditions import (
    Action,
    StopConditions,
    in_trading_window,
    load_stop_conditions,
    parse_duration,
)
from trade_agent.risk.guard import (
    Hit,
    PreTradeContext,
    RiskSnapshot,
    evaluate,
    pre_trade_violations,
)
from trade_agent.risk.state import GLOBAL, OpState, ScopeState, combined, escalate
from trade_agent.strategy.portfolio import TradeIdea
from trade_agent.strategy.profiles import load_strategy_config

D = Decimal
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
REPOSITORY_FILE = Path(__file__).parents[3] / "config" / "stop_conditions.yaml"
PROFILES = load_strategy_config(Path(__file__).parents[2] / "fixtures" / "profiles.yaml")


# ============================================================================ condições
def test_repository_stop_conditions_are_valid() -> None:
    conditions = load_stop_conditions(REPOSITORY_FILE)
    g = conditions.global_
    assert g.max_daily_loss_pct is not None and g.max_daily_loss_pct.cooldown == timedelta(hours=24)
    assert g.max_drawdown_pct is not None and g.max_drawdown_pct.action is Action.HALT
    assert g.quote_depeg_pct is not None and g.quote_depeg_pct.action is Action.FLATTEN
    assert g.api_error_rate_5m is not None and g.api_error_rate_5m.cooldown == timedelta(minutes=30)
    assert g.reconcile_mismatch is not None and g.reconcile_mismatch.value is None
    assert g.profit_target_pct is None and g.trading_window_utc is None
    assert "moderado" in conditions.per_profile


def test_empty_file_and_durations(tmp_path: Path) -> None:
    empty = tmp_path / "vazio.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_stop_conditions(empty) == StopConditions()
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration(" 15m ") == timedelta(minutes=15)
    assert parse_duration(60) == 60


@pytest.mark.parametrize("window", ["9:00-10:00", "25:00-26:00", "x"])
def test_invalid_trading_windows(window: str) -> None:
    with pytest.raises(ValidationError, match="janela inválida"):
        StopConditions.model_validate({"global": {"trading_window_utc": [window]}})


def test_trading_windows() -> None:
    assert in_trading_window(None, NOW)
    assert in_trading_window(["11:00-13:00"], NOW)
    assert not in_trading_window(["13:00-14:00"], NOW)
    assert in_trading_window(["22:00-02:00"], NOW.replace(hour=23))
    assert in_trading_window(["13:00-14:00", "22:00-12:00"], NOW)
    assert not in_trading_window(["22:00-02:00"], NOW)


# ============================================================================ estados
def test_scope_state_serialization_and_cooldown() -> None:
    paused = ScopeState(OpState.PAUSED, "x", NOW, NOW + timedelta(hours=1))
    assert ScopeState.from_json(paused.to_json()) == paused
    assert ScopeState.from_json(ScopeState().to_json()) == ScopeState()
    assert paused.current(NOW) == paused
    assert paused.current(NOW + timedelta(hours=1)) == ScopeState()
    manual = ScopeState(OpState.PAUSED, "manual", NOW)
    assert manual.current(NOW + timedelta(days=30)) == manual


def test_escalation_rules() -> None:
    running, halted = ScopeState(), ScopeState(OpState.HALTED)
    short = ScopeState(OpState.PAUSED, until=NOW + timedelta(hours=1))
    long = ScopeState(OpState.PAUSED, until=NOW + timedelta(hours=4))
    forever = ScopeState(OpState.PAUSED)
    assert escalate(running, short) == short
    assert escalate(short, long) == long  # estende
    assert escalate(long, short) is None
    assert escalate(short, forever) == forever
    assert escalate(forever, long) is None
    assert escalate(halted, long) is None  # nunca alivia
    assert escalate(halted, ScopeState(OpState.FLATTENING)) is not None
    already_flat = ScopeState(OpState.HALTED, flattened=True)
    assert escalate(already_flat, ScopeState(OpState.FLATTENING)) is None  # não repete
    assert ScopeState.from_json(already_flat.to_json()) == already_flat
    assert combined(running, short, halted) is OpState.HALTED
    assert OpState.RUNNING.allows_entries and not OpState.PAUSED.allows_entries
    assert OpState.PAUSED.allows_rule_exits and not OpState.HALTED.allows_rule_exits


# ============================================================================ gatilhos
def _snapshot(**overrides: Any) -> RiskSnapshot:
    return snapshot(**overrides)


def test_calm_snapshot_triggers_nothing() -> None:
    assert evaluate(CONDITIONS, _snapshot()) == []
    missing = _snapshot(btc_change_1h=None, quote_deviation=None, fear_greed=None)
    assert evaluate(CONDITIONS, missing) == []


@pytest.mark.parametrize(("condition", "scope", "overrides", "action"), TRIGGERS)
def test_each_trigger_fires_its_action(
    condition: str, scope: str, overrides: dict[str, Any], action: Action
) -> None:
    hits = evaluate(CONDITIONS, _snapshot(**overrides))
    assert [(h.scope, h.condition, h.action) for h in hits] == [(scope, condition, action)]
    assert condition in hits[0].reason


def test_thresholds_are_inclusive_and_directional() -> None:
    assert evaluate(CONDITIONS, _snapshot(btc_change_1h=-0.059)) == []
    assert evaluate(CONDITIONS, _snapshot(btc_change_1h=0.08)) == []  # alta não pausa
    assert evaluate(CONDITIONS, _snapshot(fear_greed=10)) == []
    assert evaluate(CONDITIONS, _snapshot(consecutive_losses=3)) == []
    rally = CONDITIONS.model_copy(
        update={
            "global_": CONDITIONS.global_.model_copy(
                update={
                    "btc_move_1h_pct": CONDITIONS.global_.btc_move_1h_pct.model_copy(  # type: ignore[union-attr]
                        update={"value": 5.0}
                    ),
                    "profit_target_pct": CONDITIONS.global_.max_drawdown_pct,
                }
            )
        }
    )
    hits = evaluate(rally, _snapshot(btc_change_1h=0.05, equity=D(1200), peak_equity=D(1200)))
    assert {h.condition for h in hits} == {"btc_move_1h_pct", "profit_target_pct"}


def test_profile_triggers_and_zero_capital() -> None:
    conditions = StopConditions.model_validate(
        {
            "per_profile": {
                "conservador": {"max_consecutive_losses": {"value": 3, "action": "halt"}},
                "fantasma": {"max_daily_loss_pct": {"value": 1, "action": "pause"}},
            }
        }
    )
    snapshot = _snapshot(profile_consecutive_losses={"conservador": 3})
    (hit,) = evaluate(conditions, snapshot)
    assert (hit.scope, hit.action, hit.threshold) == ("conservador", Action.HALT, 3)
    assert Hit(GLOBAL, "x", Action.PAUSE, 1.0, None).reason == "x: 1"


# ============================================================================ pré-ordem
def _idea(**policy: Any) -> TradeIdea:
    values: dict[str, Any] = {
        "take_profit_mode": TakeProfitMode.TRAILING,
        "take_profit_pct": D("10.8"),
        "stop_mode": StopMode.FIXED,
        "take_profit_trailing_bips": 130,
        "stop_pct": D(4),
    }
    values.update(policy)
    return TradeIdea(
        profile="conservador",
        profile_code="con",
        symbol="BTCUSDT",
        tier=Tier.CORE,
        setup="breakout",
        score=D("0.7"),
        entry=EntryOrder("BTCUSDT", D("0.001"), D(63000)),
        policy=ProtectionPolicy(**values),
        notional=D(63),
        risk=D("2.5"),
    )


def _violations(
    idea: TradeIdea, conditions: StopConditions = CONDITIONS, **context: Any
) -> list[str]:
    ctx = PreTradeContext(**{"state": OpState.RUNNING, "now": NOW, **context})
    return pre_trade_violations(
        idea,
        profile=PROFILES.profiles["conservador"],
        capital=D(500),
        rules=rules_for("BTCUSDT"),
        context=ctx,
        conditions=conditions,
    )


def test_pre_trade_accepts_a_sound_idea() -> None:
    assert _violations(_idea()) == []


def test_pre_trade_rejections() -> None:
    windowed = StopConditions.model_validate({"global": {"trading_window_utc": ["00:00-01:00"]}})
    assert _violations(_idea(), state=OpState.PAUSED) == ["estado operacional paused"]
    assert _violations(_idea(), windowed) == ["fora da janela de negociação"]
    assert _violations(_idea(), active_symbols=frozenset({"BTCUSDT"})) == [
        "já há posição ativa no ativo"
    ]
    assert _violations(_idea(), vetoed_assets=frozenset({"BTC"})) == ["vetado pelo analista"]
    assert _violations(_idea(), delisted_symbols=frozenset({"BTCUSDT"})) == ["em delistagem"]
    assert _violations(_idea(take_profit_pct=D(3))) == ["R:R 0.67 abaixo de 1.5"]
    risky = replace(_idea(), risk=D(3))
    assert _violations(risky) == ["risco 3.00 acima do limite 2.62"]
    bad_trailing = _violations(_idea(take_profit_trailing_bips=5))
    assert len(bad_trailing) == 1 and "TRAILING_DELTA" in bad_trailing[0]
    trailing_stop = _idea(
        stop_mode=StopMode.TRAILING, stop_pct=None, stop_trailing_bips=300, take_profit_pct=D(8)
    )
    assert _violations(trailing_stop) == []
