"""The ``lead_consensus`` profile (D-052): its schema, the repository's one and the shares
it took."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.support.local_profiles import LOCAL_PULLBACK, local_profile, merge, raw_config
from trade_agent.risk.conditions import load_stop_conditions
from trade_agent.signals import ConsensusParams, Family
from trade_agent.strategy.profiles import (
    LeadConsensusConfig,
    LeadsConfig,
    LeadSelectionConfig,
    StrategyConfig,
    load_strategy_config,
)

D = Decimal
REPOSITORY_FILE = Path(__file__).parents[3] / "config" / "profiles.yaml"
REPOSITORY = load_strategy_config(REPOSITORY_FILE)
LEAD: dict[str, Any] = {"family": "lead_consensus", "signals": {}, "leads": {}}


def test_the_repository_profile_follows_spot_lead_traders_on_a_tenth_of_the_account() -> None:
    profile = REPOSITORY.profiles["lead_consensus_1h"]
    assert profile.enabled and profile.family is Family.LEAD_CONSENSUS
    assert (profile.code, profile.timeframe, profile.capital_share) == ("lead1h", "1h", D("0.10"))
    assert REPOSITORY.profile_capital("lead_consensus_1h") == D(100)
    assert profile.local_params() == ConsensusParams(
        max_extension_atr=1.5, min_stop_atr=2.0, max_stop_pct=0.06
    )
    assert not profile.llm.enabled and not profile.uses_regime_filter
    assert profile.risk is not None and profile.risk.max_drawdown_pct == D(8)
    # the file spells out the defaults, so a change of either is a deliberate one
    assert profile.leads == LeadsConfig()


def test_the_enabled_shares_still_add_up_to_the_whole_account() -> None:
    shares = {name: p.capital_share for name, p in REPOSITORY.enabled_profiles().items()}
    assert sum(shares.values()) == 1
    # D-052: half of each rejected pair-local profile; nobody else gave anything
    assert shares == {
        "swing_trend": D("0.35"),
        "momentum_alpha": D("0.30"),
        "alt_atr": D("0.05"),
        "pair_pullback_1h": D("0.10"),
        "pair_breakout_1h": D("0.05"),
        "pair_range_1h": D("0.05"),
        "lead_consensus_1h": D("0.10"),
    }


def test_the_repository_profile_passes_the_risk_guard_at_any_stop() -> None:
    """Its target is 2R net of costs, so the minimum R:R holds from the tightest stop to the
    cap, and its smallest lot is above Binance's 5 USDT minimum."""
    profile = REPOSITORY.profiles["lead_consensus_1h"]
    pre_trade = load_stop_conditions(REPOSITORY_FILE.with_name("stop_conditions.yaml")).pre_trade
    fee = D(str(pre_trade.round_trip_fee_pct))
    capital = REPOSITORY.profile_capital("lead_consensus_1h")
    for stop in (D("0.5"), D(2), D(4), D(6), D(9)):
        policy = profile.protection.policy(stop, D(1), cost_pct=profile.cost_pct())
        assert policy.stop_pct is not None and policy.stop_pct <= D(6)
        reward_risk = (policy.take_profit_pct - fee) / (policy.stop_pct + fee)
        assert reward_risk >= D(str(pre_trade.min_reward_risk)), stop
        risk = capital * profile.allocation.risk_per_trade_pct / 100
        assert risk / ((policy.stop_pct + profile.cost_pct()) / 100) >= D(5)


def test_the_family_requires_its_leads_block_and_only_it_may_have_one() -> None:
    profile = local_profile(**LEAD)
    assert profile.leads == LeadsConfig() and profile.family.needs_leads
    with pytest.raises(ValidationError, match="exige leads"):
        local_profile(family="lead_consensus", signals={})
    with pytest.raises(ValidationError, match="só da família lead_consensus"):
        local_profile(leads={})
    data = raw_config()
    data["profiles"]["moderado"]["leads"] = {}
    with pytest.raises(ValidationError, match="só da família lead_consensus"):
        StrategyConfig.model_validate(data)


def test_it_is_a_pair_local_family_with_the_same_obligations() -> None:
    looser = merge(LEAD, {"signals": {"max_extension_atr": 2.0}})
    assert local_profile(**looser).local_params() == (
        ConsensusParams(max_extension_atr=2.0, min_stop_atr=1.5, max_stop_pct=0.06)
    )
    for overrides, message in (
        ({"signals": {"rsi_dip": 40}}, "desconhecidos"),
        ({"signals": {"min_stop_atr": 3}}, "protection.stop"),
        ({"llm": {"enabled": True}}, "llm.enabled false"),
        ({"risk": None}, "risk"),
    ):
        with pytest.raises(ValidationError, match=message):
            local_profile(**merge(LEAD, overrides))


def test_durations_accept_minutes_hours_and_days() -> None:
    config = LeadsConfig.model_validate(
        {
            "poll_interval": "5m",
            "ranking_interval": "12h",
            "max_data_age": "20m",
            "max_ranking_age": "2d",
            "backoff": "30m",
            "selection": {"max_idle": "14d"},
            "consensus": {"window": "36h"},
        }
    )
    assert (config.poll_interval, config.max_data_age) == (
        timedelta(minutes=5),
        timedelta(minutes=20),
    )
    assert (config.ranking_interval, config.max_ranking_age) == (
        timedelta(hours=12),
        timedelta(days=2),
    )
    assert config.backoff == timedelta(minutes=30)
    assert config.selection.max_idle == timedelta(days=14)
    assert config.consensus.window == timedelta(hours=36)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"poll_interval": "4m"}, "poll_interval deve ser >= 5m"),
        ({"max_data_age": "10m"}, "max_data_age deve ser >= poll_interval"),
        ({"ranking_interval": "2d"}, "max_ranking_age deve ser >= ranking_interval"),
        ({"selection": {"shortlist": 3}}, "shortlist deve ser >= max_leads"),
        ({"selection": {"max_drawdown_pct": 0}}, "max_drawdown_pct"),
        ({"selection": {"max_copier_loss_pct": -1}}, "max_copier_loss_pct"),
        ({"consensus": {"min_backers": 0}}, "min_backers"),
        ({"consensus": {"min_buy_pct": 0}}, "min_buy_pct"),
        ({"consensus": {"follow": "everyone"}}, "follow"),
        ({"request_delay_s": 60}, "request_delay_s"),
    ],
)
def test_invalid_lead_settings(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        LeadsConfig.model_validate(values)


def test_the_defaults_never_let_one_trader_or_the_return_alone_decide() -> None:
    selection, consensus = LeadSelectionConfig(), LeadConsensusConfig()
    assert consensus.min_backers >= 2
    assert selection.max_copier_loss_pct is not None and selection.min_sharpe > 0
    assert selection.min_positive_weeks_pct > 50 and selection.max_drawdown_pct <= 20
    assert LeadsConfig().max_data_age < timedelta(hours=1)  # stale trades are no signal
    assert LOCAL_PULLBACK["family"] != "lead_consensus"  # the fixture stays a pullback
