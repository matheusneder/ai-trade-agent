"""Pair-local profiles (D-044): schema, parameters, protection policy and entry planning."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.support.local_profiles import local_config, local_profile, raw_config
from tests.unit.strategy.test_portfolio_and_exits import CONSERVATIVE, _candidate
from trade_agent.execution.orders import TakeProfitMode
from trade_agent.market.universe import Tier
from trade_agent.signals import BreakoutParams, Family, PullbackParams, RangeParams
from trade_agent.strategy.exits import exit_reason
from trade_agent.strategy.portfolio import Holding, plan_entries
from trade_agent.strategy.profiles import StrategyConfig, TakeProfitConfig, load_strategy_config

D = Decimal
REPOSITORY_FILE = Path(__file__).parents[3] / "config" / "profiles.yaml"


def test_repository_local_profiles_are_disabled_seeds() -> None:
    config = load_strategy_config(REPOSITORY_FILE)
    local = {n: p for n, p in config.profiles.items() if p.family.local}
    assert set(local) == {"pair_pullback_1h", "pair_breakout_1h", "pair_range_1h"}
    assert not any(p.enabled for p in local.values())
    assert not set(local) & set(config.enabled_profiles())
    expected = {
        "pair_pullback_1h": PullbackParams(min_stop_atr=1.5, max_stop_pct=0.06),
        "pair_breakout_1h": BreakoutParams(min_stop_atr=2.0, max_stop_pct=0.07),
        "pair_range_1h": RangeParams(min_stop_atr=1.5, max_stop_pct=0.04),
    }
    for name, profile in local.items():
        assert profile.local_params() == expected[name]
        assert not profile.uses_regime_filter
        assert not profile.llm.enabled
        assert profile.risk is not None and profile.risk.cooldown == timedelta(hours=24)
    for name in ("swing_trend", "momentum_alpha", "alt_atr"):
        assert config.profiles[name].family is Family.LEGACY
        assert config.profiles[name].uses_regime_filter


def test_a_local_profile_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError, match="só de laboratório"):
        local_config(local={"enabled": True})


def test_each_family_has_its_own_parameters() -> None:
    profile = local_profile(signals={"rsi_dip": 45})
    assert profile.local_params() == PullbackParams(rsi_dip=45, min_stop_atr=1.5, max_stop_pct=0.06)
    assert profile.cost_pct(D(9)) == D("0.3")
    assert CONSERVATIVE.cost_pct(D(9)) == D(9)
    with pytest.raises(ValueError, match="local_params"):
        profile.signal_params()
    with pytest.raises(ValueError, match="signal_params"):
        CONSERVATIVE.local_params()
    no_filter = CONSERVATIVE.model_copy(update={"signals": {"use_regime_filter": False}})
    assert CONSERVATIVE.uses_regime_filter and not no_filter.uses_regime_filter


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"signals": {"rsi_pullback_max": 40}}, "desconhecidos"),
        ({"signals": {"max_stop_pct": 0.1}}, "protection.stop"),
        ({"llm": {"enabled": True}}, "llm.enabled false"),
        ({"protection": {"break_even_after_r": 1.0}}, "sem break_even_after_r"),
        (
            {"protection": {"stop": {"mode": "trailing", "trailing_delta_bps": 300}}},
            "stop fixo",
        ),
        ({"exits": {"exit_score": -0.5}}, "sem exits"),
        ({"risk": None}, "risk"),
        (
            {
                "family": "legacy",
                "llm": {"enabled": True},
                "signals": {},
                "protection": {"take_profit": {"activation_r": None}},
            },
            "só das famílias locais",
        ),
        ({"family": "legacy", "protection": {"take_profit": {"activation_r": None}}}, "locais"),
    ],
)
def test_invalid_local_profiles(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        local_profile(**overrides)


def test_a_legacy_profile_cannot_use_the_local_take_profit() -> None:
    data = raw_config()
    data["profiles"]["moderado"]["protection"]["take_profit"]["activation_r"] = 2.0
    with pytest.raises(ValidationError, match="só das famílias locais"):
        StrategyConfig.model_validate(data)


@pytest.mark.parametrize(
    ("take_profit", "message"),
    [
        ({"mode": "trailing", "activation_pct": 1}, "trailing_delta_bps ou trailing_atr_mult"),
        (
            {
                "mode": "trailing",
                "activation_pct": 1,
                "trailing_delta_bps": 100,
                "trailing_atr_mult": 1,
            },
            "trailing_delta_bps ou trailing_atr_mult",
        ),
        ({"mode": "limit", "activation_pct": 1, "trailing_atr_mult": 1}, "não usa trailing"),
        (
            {"mode": "limit", "activation_pct": 1, "activation_r": 1, "signal_target": True},
            "só um de",
        ),
        (
            {
                "mode": "trailing",
                "activation_pct": 1,
                "trailing_delta_bps": 100,
                "signal_target": True,
            },
            "signal_target exige",
        ),
    ],
)
def test_invalid_local_take_profit(take_profit: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        TakeProfitConfig.model_validate(take_profit)


def test_trailing_from_the_atr_is_rounded_and_kept_in_binances_range() -> None:
    config = TakeProfitConfig(
        mode=TakeProfitMode.TRAILING, activation_pct=D(1), trailing_atr_mult=1.0
    )
    assert config.local_only
    assert config.trailing_bips_for(D("1.234")) == 123
    assert config.trailing_bips_for(D("0.01")) == 10
    assert config.trailing_bips_for(D("40")) == 2000
    assert config.trailing_bips_for(None) == 10
    fixed = TakeProfitConfig(
        mode=TakeProfitMode.TRAILING, activation_pct=D(1), trailing_delta_bps=150
    )
    assert fixed.trailing_bips_for(D(5)) == 150 and not fixed.local_only


def test_policy_of_a_net_r_target_and_of_a_signal_target() -> None:
    protection = local_profile().protection
    policy = protection.policy(D(2), D(1), cost_pct=D("0.3"))
    assert policy.take_profit_mode is TakeProfitMode.LIMIT
    assert policy.take_profit_pct == D("1.5") * (D(2) + D("0.3")) + D("0.3")
    assert policy.stop_pct == D(2) and policy.take_profit_trailing_bips is None
    # the floor (activation_pct) and the stop cap
    assert protection.policy(D("0.01"), D(1)).take_profit_pct == D("0.5")
    capped = protection.policy(D(9), D(1), cost_pct=D(0))
    assert capped.stop_pct == D(6) and capped.take_profit_pct == D(9)
    trailing = local_profile(
        protection={
            "take_profit": {"mode": "trailing", "activation_r": 2.5, "trailing_atr_mult": 1}
        }
    ).protection.policy(D(2), D("1.5"), cost_pct=D(0))
    assert (trailing.take_profit_pct, trailing.take_profit_trailing_bips) == (D(5), 150)
    target = local_profile(
        protection={"take_profit": {"activation_r": None, "signal_target": True}}
    ).protection
    assert target.policy(D(1), D(1), target_pct=D("2.5")).take_profit_pct == D("2.5")
    assert target.policy(D(1), D(1)).take_profit_pct == D("0.5")  # no target: the floor


def _local_plan(profile_overrides: dict[str, Any] | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    profile = local_profile(**(profile_overrides or {}))
    args: dict[str, Any] = {
        "name": "local",
        "profile": profile,
        "capital": D(100),
        "holdings": [],
        "candidates": [],
    }
    args.update(kw)
    return plan_entries(**args)


def test_local_sizing_uses_the_profiles_cost_and_its_net_target() -> None:
    candidate = _candidate("SOLUSDT", setup="pair_pullback", stop_pct=0.03, price="100")
    (idea,) = _local_plan(candidates=[candidate], execution_cost_pct=D(5)).ideas
    # 100 × 0.5% ÷ (3% + 0.3%) ≈ 15.15 USDT, the example of the specification
    assert idea.notional == pytest.approx(D("15.15"), abs=D("0.2"))
    assert idea.risk == pytest.approx(D("0.5"), abs=D("0.01"))
    assert idea.policy.take_profit_pct == D("1.5") * D("3.3") + D("0.3")


def test_a_stale_signal_is_not_bought() -> None:
    signal = replace(_candidate("SOLUSDT").signal, max_entry_price=99.5)
    stale = replace(_candidate("SOLUSDT", price="100"), signal=signal)
    plan = _local_plan(candidates=[stale])
    assert plan.ideas == ()
    assert plan.rejections == (("SOLUSDT", "preço já avançou além do limite do sinal"),)
    fresh = replace(stale, signal=replace(signal, max_entry_price=100.5))
    assert len(_local_plan(candidates=[fresh]).ideas) == 1


def test_the_open_initial_risk_of_the_profile_is_capped() -> None:
    held = Holding("local", "ETHUSDT", Tier.CORE, D(15), risk=D("0.6"))
    other = Holding("conservador", "BNBUSDT", Tier.CORE, D(500), risk=D(50))
    first, second = _candidate("SOLUSDT", stop_pct=0.03), _candidate("ADAUSDT", stop_pct=0.03)
    plan = _local_plan(candidates=[first, second], holdings=[held, other])
    # 1% of 100 USDT: 0.6 already open, a new entry risks 0.5
    assert plan.ideas == ()
    assert ("SOLUSDT", "risco aberto do perfil acima do limite") in plan.rejections
    two = _local_plan(candidates=[first, second])
    assert len(two.ideas) == 2  # 0.5 + 0.5 = 1%


def test_a_range_target_comes_from_the_signal() -> None:
    signal = replace(_candidate("SOLUSDT").signal, setup="pair_range", target_pct=0.025)
    candidate = replace(_candidate("SOLUSDT"), signal=signal)
    overrides = {"protection": {"take_profit": {"activation_r": None, "signal_target": True}}}
    (idea,) = _local_plan(overrides, candidates=[candidate]).ideas
    assert idea.policy.take_profit_pct == D("2.5")


def test_a_technical_exit_comes_after_the_veto_and_the_holding_time() -> None:
    profile = local_profile()
    reason = "saída técnica"
    assert exit_reason(profile=profile, age=None, weak_cycles=0, technical=reason) == reason
    assert exit_reason(
        profile=profile, age=timedelta(hours=48), weak_cycles=0, technical=reason
    ) == ("tempo máximo de permanência")
    assert exit_reason(profile=profile, age=None, weak_cycles=0, veto=True, technical=reason) == (
        "veto do analista"
    )
    assert exit_reason(profile=profile, age=None, weak_cycles=0) is None
