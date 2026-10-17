from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from trade_agent.execution.orders import StopMode, TakeProfitMode
from trade_agent.market.universe import Tier
from trade_agent.risk.conditions import load_stop_conditions
from trade_agent.signals import SignalParams
from trade_agent.strategy.profiles import StrategyConfig, load_strategy_config

D = Decimal
CONFIG_FILE = Path(__file__).parents[2] / "fixtures" / "profiles.yaml"
REPOSITORY_FILE = Path(__file__).parents[3] / "config" / "profiles.yaml"


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    return data


def test_repository_profiles_are_valid_and_within_exchange_limits() -> None:
    config = load_strategy_config(REPOSITORY_FILE)
    enabled = list(config.enabled_profiles())
    assert enabled
    assert sum(config.profiles[name].capital_share for name in enabled) <= 1
    for name in enabled:
        protection = config.profiles[name].protection
        policy = protection.policy(D("2"))
        for bips in (policy.take_profit_trailing_bips, policy.stop_trailing_bips):
            assert bips is None or 10 <= bips <= 2000  # TRAILING_DELTA filter
        assert protection.stop_distance_pct(D("100")) <= 10


def _reward_risk(name: str, atr_pct: Decimal) -> tuple[Decimal, bool]:
    """R:R checked by the Risk Guard for an entry of the repository profile ``name`` with
    ``atr_pct`` (percentage), and whether the stop is at its cap."""
    pre_trade = load_stop_conditions(REPOSITORY_FILE.with_name("stop_conditions.yaml")).pre_trade
    fee = D(str(pre_trade.round_trip_fee_pct))
    profile = load_strategy_config(REPOSITORY_FILE).profiles[name]
    params = profile.signal_params()
    cap = D(str(params.max_stop_pct)) * 100
    stop = max(atr_pct * D(str(params.atr_stop_mult)), D(str(params.min_stop_pct)) * 100)
    policy = profile.protection.policy(
        profile.protection.stop_distance_pct(min(stop, cap)), atr_pct
    )
    stop_pct = policy.stop_pct or D(policy.stop_trailing_bips or 0) / 100
    return (policy.take_profit_pct - fee) / (stop_pct + fee), stop >= cap


def test_repository_profiles_pass_the_minimum_reward_risk() -> None:
    """The Risk Guard refuses entries below ``pre_trade.min_reward_risk``: below the stop cap,
    the repository profiles must not be blocked by their own stop and take-profit settings."""
    pre_trade = load_stop_conditions(REPOSITORY_FILE.with_name("stop_conditions.yaml")).pre_trade
    minimum = D(str(pre_trade.min_reward_risk))
    for name in load_strategy_config(REPOSITORY_FILE).enabled_profiles():
        for atr_pct in (D("0.3"), D("0.8"), D("1.2"), D("1.8"), D("2.2"), D("2.5"), D("4"), D("7")):
            reward_risk, capped = _reward_risk(name, atr_pct)
            if name == "alt_atr" and capped:
                continue  # refused on purpose, see the next test
            assert reward_risk >= minimum, (name, atr_pct, reward_risk)


def test_alt_atr_skips_entries_with_the_stop_at_its_cap() -> None:
    """D-036: with the stop at its 7% cap (ATR above ~2.3%), the fixed 10.9% activation is below
    the minimum R:R, so the Risk Guard refuses the most volatile entries, as in the lab."""
    pre_trade = load_stop_conditions(REPOSITORY_FILE.with_name("stop_conditions.yaml")).pre_trade
    reward_risk, capped = _reward_risk("alt_atr", D("3"))
    assert capped and reward_risk < D(str(pre_trade.min_reward_risk))


def test_fixture_profiles_are_valid() -> None:
    config = load_strategy_config(CONFIG_FILE)
    assert list(config.enabled_profiles()) == ["conservador", "moderado"]
    assert config.profile_capital("conservador") == D("500")
    conservative = config.profiles["conservador"]
    assert conservative.timeframe == "4h"
    assert conservative.tier_limit(Tier.CORE) == D("0.8")
    assert conservative.tier_limit(Tier.SMALL) == 0
    assert conservative.protection.max_holding == timedelta(days=21)
    assert config.profiles["moderado"].timeframe == "1h"


def test_fixed_stop_policy_is_capped_by_max_pct() -> None:
    protection = load_strategy_config(CONFIG_FILE).profiles["conservador"].protection
    policy = protection.policy(D("6"))
    assert policy.stop_mode is StopMode.FIXED
    assert policy.stop_pct == D("4")
    assert policy.take_profit_mode is TakeProfitMode.TRAILING
    assert policy.take_profit_trailing_bips == 100
    assert protection.stop_distance_pct(D("2.5")) == D("2.5")
    assert protection.stop_distance_pct(D("9")) == D("4")


def test_take_profit_activation_is_fixed_by_default() -> None:
    protection = load_strategy_config(CONFIG_FILE).profiles["conservador"].protection
    assert protection.take_profit.activation_for(D("5")) == D("3")
    assert protection.policy(D("2"), atr_pct=D("5")).take_profit_pct == D("3")


def test_take_profit_activation_from_atr_is_clipped() -> None:
    data = _raw()
    data["profiles"]["conservador"]["protection"]["take_profit"].update(
        {"activation_pct": 7, "activation_atr_mult": 5, "activation_max_pct": 16}
    )
    protection = StrategyConfig.model_validate(data).profiles["conservador"].protection
    take_profit = protection.take_profit
    assert take_profit.activation_for(D("2")) == D("10")
    assert take_profit.activation_for(D("1")) == D("7")  # floor
    assert take_profit.activation_for(D("4")) == D("16")  # cap
    assert take_profit.activation_for(None) == D("7")  # unknown ATR: floor
    assert take_profit.activation_for(D("0")) == D("7")
    assert protection.policy(D("2"), atr_pct=D("2.4")).take_profit_pct == D("12.0")


def test_trailing_stop_policy_and_distance() -> None:
    protection = load_strategy_config(CONFIG_FILE).profiles["agressivo"].protection
    policy = protection.policy(D("3"))
    assert policy.stop_mode is StopMode.TRAILING
    assert policy.stop_trailing_bips == 800
    assert protection.stop_distance_pct(D("3")) == D("8")
    assert protection.max_holding == timedelta(days=7)


def test_signal_params_overrides() -> None:
    data = _raw()
    data["profiles"]["moderado"]["signals"] = {
        "adx_min": 25,
        "rsi_pullback_max": 40,
        "use_regime_filter": False,
    }
    params = StrategyConfig.model_validate(data).profiles["moderado"].signal_params()
    assert params.adx_min == 25
    assert params.rsi_pullback_max == 40
    assert params.use_regime_filter is False


def test_fixed_stop_drives_signal_atr_multiple_and_cap() -> None:
    config = load_strategy_config(CONFIG_FILE)
    moderate = config.profiles["moderado"].signal_params()  # stop: atr 2.5, max 7%
    assert moderate.atr_stop_mult == 2.5
    assert moderate.max_stop_pct == pytest.approx(0.07)
    trailing = config.profiles["agressivo"].signal_params()  # trailing stop: defaults
    assert trailing.atr_stop_mult == SignalParams().atr_stop_mult
    assert trailing.max_stop_pct == SignalParams().max_stop_pct


def test_max_holding_formats() -> None:
    data = _raw()
    data["profiles"]["moderado"]["protection"]["max_holding"] = "36h"
    assert StrategyConfig.model_validate(data).profiles[
        "moderado"
    ].protection.max_holding == timedelta(hours=36)
    data["profiles"]["moderado"]["protection"]["max_holding"] = 3600
    assert StrategyConfig.model_validate(data).profiles[
        "moderado"
    ].protection.max_holding == timedelta(hours=1)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("profiles", "moderado", "capital_share"), 0.6, "capital_share"),
        (("profiles", "moderado", "code"), "con", "repetidos"),
        (("profiles", "moderado", "code"), "Mod", "pattern"),
        (("profiles", "moderado", "timeframe"), "2h", "timeframe"),
        (("profiles", "moderado", "signals"), {"nao_existe": 1}, "desconhecidos"),
        (("profiles", "moderado", "signals"), {"atr_stop_mult": 3}, "protection.stop"),
        (("profiles", "moderado", "protection", "stop"), {"mode": "fixed"}, "atr_mult"),
        (
            ("profiles", "moderado", "protection", "stop"),
            {"mode": "trailing"},
            "trailing_delta_bps",
        ),
        (
            ("profiles", "moderado", "protection", "take_profit"),
            {"mode": "trailing", "activation_pct": 3},
            "trailing_delta_bps",
        ),
        (
            ("profiles", "moderado", "protection", "take_profit"),
            {
                "mode": "trailing",
                "activation_pct": 3,
                "trailing_delta_bps": 100,
                "activation_atr_mult": 4,
            },
            "activation_max_pct",
        ),
        (
            ("profiles", "moderado", "protection", "take_profit"),
            {
                "mode": "trailing",
                "activation_pct": 3,
                "trailing_delta_bps": 100,
                "activation_atr_mult": 4,
                "activation_max_pct": 2,
            },
            ">= activation_pct",
        ),
        (("profiles", "moderado", "allocation", "max_position_pct"), 1.5, "less than or equal"),
        (("profiles", "moderado", "extra"), 1, "Extra inputs"),
    ],
)
def test_invalid_configurations(path: tuple[str, ...], value: object, message: str) -> None:
    data = _raw()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError, match=message):
        StrategyConfig.model_validate(data)
