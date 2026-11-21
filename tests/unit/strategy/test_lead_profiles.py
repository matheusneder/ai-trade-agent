"""The ``lead_consensus`` profile (D-052): its schema, the repository's one, the shares it
took, its decision cycle (D-053) and the profiles that test its flow (D-054, D-056)."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.support.local_profiles import LOCAL_PULLBACK, local_profile, merge, raw_config
from tests.unit.strategy.test_portfolio_and_exits import _candidate
from trade_agent.market.universe import Tier
from trade_agent.risk.conditions import load_stop_conditions
from trade_agent.signals import ConsensusParams, Family
from trade_agent.strategy.portfolio import (
    NO_APPROVAL,
    Candidate,
    Holding,
    MarketOpinion,
    plan_entries,
)
from trade_agent.strategy.profiles import (
    LeadConsensusConfig,
    LeadsConfig,
    LeadSelectionConfig,
    ScopeRiskConfig,
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
    # D-059: half of its 0.20 went to lead_consensus_llm_4h
    assert (profile.code, profile.timeframe, profile.capital_share) == ("lead1h", "1h", D("0.10"))
    assert REPOSITORY.profile_capital("lead_consensus_1h") == D(100)
    # D-055: at each 1h close again (every 10 minutes in D-053), with one reading per cycle
    assert (profile.cycle, profile.cadence) == (timedelta(hours=1), "60m")
    assert profile.local_params() == ConsensusParams(
        max_extension_atr=1.5, min_stop_atr=2.0, max_stop_pct=0.06
    )
    assert not profile.llm.enabled and not profile.uses_regime_filter
    assert profile.risk is not None and profile.risk.max_drawdown_pct == D(8)
    # the file spells out the defaults, so a change of any is a deliberate one: D-055 reads
    # the traders every hour, so their reading counts for 90 minutes
    hourly = LeadsConfig(poll_interval=timedelta(hours=1), max_data_age=timedelta(minutes=90))
    assert profile.leads == hourly


def test_the_4h_profile_loosens_its_own_rules_and_waits_for_the_analysts_approval() -> None:
    """D-059: lead_consensus_1h's mechanism on the 4h candles, each rule of its own one step
    looser, and no entry without the analyst's explicit opinion."""
    profile = REPOSITORY.profiles["lead_consensus_llm_4h"]
    strict = REPOSITORY.profiles["lead_consensus_1h"]
    assert profile.enabled and profile.family is Family.LEAD_CONSENSUS
    assert (profile.code, profile.capital_share) == ("leadllm4h", strict.capital_share)
    # at each 4h close, with the legacy profiles whose research it shares
    assert (profile.timeframe, profile.cycle, profile.cadence) == ("4h", None, "4h")
    assert profile.local_params() == ConsensusParams(
        max_extension_atr=2.5, close_above_ema=False, min_stop_atr=2.0, max_stop_pct=0.08
    )
    llm = profile.llm
    assert llm.requires_approval and llm.on_failure == "pause_entries"
    assert (llm.weight, llm.min_confidence) == (D("0.5"), D("0.5"))
    # the same values serve the other mode: only `validation` changes
    assert not llm.model_copy(update={"validation": "filter"}).requires_approval
    assert profile.risk is not None and strict.risk is not None and strict.leads is not None
    slots, risk = profile.allocation.max_open_positions, profile.allocation.risk_per_trade_pct
    assert profile.risk.max_open_risk_pct == slots * risk  # every slot fits the open-risk cap
    # a wider budget than the 1h profile's, with every pause of it still there
    assert profile.risk == strict.risk.model_copy(
        update={
            "max_open_risk_pct": D("2.25"),
            "daily_loss_pct": D("2.5"),
            "consecutive_losses": 4,
            "cooldown": timedelta(hours=12),
            "max_drawdown_pct": D(10),
            "reentry_candles": 2,
        }
    )
    assert profile.leads is not None
    selection, consensus = profile.leads.selection, profile.leads.consensus
    # still by score, every performance criterion checked and never on one trader alone:
    # the 1h profile's selection but for five thresholds, its consensus but for two
    assert selection.rank_by == "score" and consensus.min_backers == 2
    assert selection == strict.leads.selection.model_copy(
        update={
            "min_days": 60,
            "min_balance": D(500),
            "min_sharpe": 0.7,
            "min_positive_weeks_pct": 50.0,
            "min_recent_gain_pct": -2.0,
        }
    )
    assert consensus == strict.leads.consensus.model_copy(
        update={"window": timedelta(hours=48), "max_premium_pct": D("2.5")}
    )


def test_a_neutral_opinion_never_fails_the_4h_profiles_minimum_score() -> None:
    """Its score is the share of the followed traders that bought, at least ``min_backers``
    of ``max_leads``; blended with a neutral opinion it must still reach ``min_score``, or
    the approval mode would refuse what the analyst approved."""
    profile = REPOSITORY.profiles["lead_consensus_llm_4h"]
    assert profile.leads is not None
    weakest = D(profile.leads.consensus.min_backers) / profile.leads.selection.max_leads
    assert (1 - profile.llm.weight) * weakest >= D(str(profile.entry.min_score)) > 0


def test_a_strong_dislike_is_recorded_as_the_analysts_refusal_not_as_a_low_score() -> None:
    """PR 70's review: with 2 of 6 traders behind a purchase (score 1/3), sentiment -0.5 at
    confidence 0.8 takes the blended score to -0.03, under ``min_score``, and the cycle
    recorded ``score abaixo do mínimo``. In approval mode the refusal is the analyst's; in
    ``filter`` mode the score is what refuses, as designed."""
    name = "lead_consensus_llm_4h"
    profile = REPOSITORY.profiles[name]

    def reasons(candidate: Candidate, **kw: Any) -> tuple[tuple[str, str], ...]:
        args: dict[str, Any] = {"profile": profile, "holdings": [], **kw}
        return plan_entries(name=name, capital=D(100), candidates=[candidate], **args).rejections

    disliked = _candidate(
        "SOLUSDT",
        score=2 / 6,
        setup="lead_consensus",
        stop_pct=0.04,
        opinion=MarketOpinion(D("-0.5"), D("0.8")),
    )
    assert disliked.final_score(profile) < D(str(profile.entry.min_score))
    assert reasons(disliked) == (("SOLUSDT", NO_APPROVAL),)
    filtering = profile.model_copy(
        update={"llm": profile.llm.model_copy(update={"validation": "filter"})}
    )
    assert reasons(disliked, profile=filtering) == (("SOLUSDT", "score abaixo do mínimo"),)
    # no reading at all (pause_entries): the reason is that, not a refusal nobody gave
    silent = replace(disliked, opinion=None)
    assert reasons(silent) == (("SOLUSDT", NO_APPROVAL),)
    assert reasons(silent, exposure_multiplier=D(0)) == (("SOLUSDT", "exposição zero"),)
    # and a held asset is not put up for approval: its reason stays its own
    held = [Holding(name, "SOLUSDT", Tier.CORE, D(10))]
    assert reasons(silent, holdings=held) == (("SOLUSDT", "ativo já em carteira"),)


def test_the_flow_test_profile_follows_the_busiest_traders_and_buys_on_one() -> None:
    """D-054: with the market down the traders of lead_consensus_1h barely trade, so this one
    follows whoever trades most often, whatever the results, on the 15m candles."""
    profile = REPOSITORY.profiles["temp_cp_trade_validation"]
    assert profile.enabled and profile.family is Family.LEAD_CONSENSUS
    assert (profile.code, profile.timeframe, profile.capital_share) == ("tcpv15m", "15m", D("0.10"))
    assert (profile.cycle, profile.cadence) == (timedelta(minutes=15), "15m")
    assert profile.leads is not None and profile.risk is not None
    selection, consensus = profile.leads.selection, profile.leads.consensus
    assert selection.rank_by == "activity"
    performance = (
        selection.min_roi_pct,
        selection.max_drawdown_pct,
        selection.min_sharpe,
        selection.max_copier_loss_pct,
        selection.min_positive_weeks_pct,
        selection.min_recent_gain_pct,
    )
    assert performance == (None,) * 6
    assert (selection.max_idle, selection.min_trades_30d) == (timedelta(days=1), 30)
    assert (consensus.min_backers, consensus.window) == (1, timedelta(hours=6))
    assert profile.leads.poll_interval == timedelta(minutes=15)  # one reading per cycle
    # the budget of the others, with a shorter pause
    others = REPOSITORY.profiles["lead_consensus_1h"].risk
    assert others is not None
    assert profile.risk == others.model_copy(update={"cooldown": timedelta(hours=4)})


def test_the_high_risk_flow_test_turns_a_consensus_into_an_order_as_often_as_it_can() -> None:
    """D-056: the 15m candles of every asset temp_cp_trade_validation saw bought were in a
    local downtrend, so it sent no order. This one loosens every filter of its own: the
    candles only discard a stop beyond 10%, and nothing pauses it."""
    profile = REPOSITORY.profiles["temp_cp_trade_high_risk_validation"]
    flow_test = REPOSITORY.profiles["temp_cp_trade_validation"]
    assert profile.enabled and profile.family is Family.LEAD_CONSENSUS
    assert (profile.code, profile.capital_share) == ("tcphr15m", D("0.10"))
    assert profile.timeframe == flow_test.timeframe == "15m"
    assert profile.cadence == flow_test.cadence == "15m"
    assert profile.local_params() == ConsensusParams(
        max_extension_atr=0, trend_filter=False, min_stop_atr=2.0, max_stop_pct=0.10
    )
    assert all(profile.tier_limit(tier) == 1 for tier in Tier)
    assert profile.allocation.cash_reserve_pct == 0
    assert profile.allocation.max_open_positions > flow_test.allocation.max_open_positions
    # no pause, no open-risk cap and no re-entry wait: only the cost of its own sizing, and
    # out of the global streak of losses
    assert profile.risk == ScopeRiskConfig(cost_pct=D("0.3"), global_streak=False)
    assert flow_test.risk is not None and flow_test.risk.global_streak
    assert profile.leads is not None and flow_test.leads is not None
    consensus = profile.leads.consensus
    assert (consensus.min_backers, consensus.window) == (1, timedelta(hours=24))
    assert consensus.max_premium_pct > flow_test.leads.consensus.max_premium_pct
    assert profile.leads.selection == flow_test.leads.selection  # the same traders


def test_the_enabled_shares_still_add_up_to_the_whole_account() -> None:
    shares = {name: p.capital_share for name, p in REPOSITORY.enabled_profiles().items()}
    assert sum(shares.values()) == 1
    # D-052 took half of each rejected pair-local profile; D-053 the rest of both, which are
    # off, and 0.10 from each main profile; D-054 lent half of it to the flow test, which
    # D-056 split with the high-risk one; D-059 gave half of the rest to the 4h profile
    assert shares == {
        "swing_trend": D("0.25"),
        "momentum_alpha": D("0.20"),
        "alt_atr": D("0.05"),
        "pair_pullback_1h": D("0.10"),
        "lead_consensus_1h": D("0.10"),
        "lead_consensus_llm_4h": D("0.10"),
        "temp_cp_trade_validation": D("0.10"),
        "temp_cp_trade_high_risk_validation": D("0.10"),
    }
    assert not REPOSITORY.profiles["pair_breakout_1h"].enabled
    assert not REPOSITORY.profiles["pair_range_1h"].enabled


@pytest.mark.parametrize(
    "name",
    [
        "lead_consensus_1h",
        "lead_consensus_llm_4h",
        "temp_cp_trade_validation",
        "temp_cp_trade_high_risk_validation",
    ],
)
def test_the_repository_profiles_pass_the_risk_guard_at_any_stop(name: str) -> None:
    """Their target is 2R net of costs, so the minimum R:R holds from the tightest stop to
    the cap, and their smallest lot is above Binance's 5 USDT minimum (D-056: 100 USDT at
    1% of risk still buys one at the 10% cap)."""
    profile = REPOSITORY.profiles[name]
    pre_trade = load_stop_conditions(REPOSITORY_FILE.with_name("stop_conditions.yaml")).pre_trade
    fee = D(str(pre_trade.round_trip_fee_pct))
    capital = REPOSITORY.profile_capital(name)
    cap = profile.protection.stop.max_pct
    for stop in (D("0.5"), D(2), D(4), D(6), D(9), D(15), D(20)):
        policy = profile.protection.policy(stop, D(1), cost_pct=profile.cost_pct())
        assert policy.stop_pct is not None and policy.stop_pct == min(stop, cap or stop)
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
        ({"protection": {"break_even_after_r": 1.0}}, "sem break_even_after_r"),
        ({"risk": None}, "risk"),
    ):
        with pytest.raises(ValidationError, match=message):
            local_profile(**merge(LEAD, overrides))


def test_it_is_the_one_local_family_that_may_have_the_analyst() -> None:
    """D-059: the pair-local families stay pure TA, as the lab ran them; this one has no lab
    run, and its analyst either only restricts or must approve each entry."""
    with pytest.raises(ValidationError, match=r"llm\.enabled false"):
        local_profile(llm={"enabled": True})
    filtered = local_profile(**merge(LEAD, {"llm": {"enabled": True}})).llm
    assert filtered.enabled and filtered.validation == "filter"
    assert not filtered.requires_approval
    approving = merge(LEAD, {"llm": {"enabled": True, "validation": "approval"}})
    assert local_profile(**approving).llm.requires_approval
    off = merge(LEAD, {"llm": {"enabled": False, "validation": "approval"}})
    assert not local_profile(**off).llm.requires_approval  # nobody to approve
    with pytest.raises(ValidationError, match="validation"):
        local_profile(**merge(LEAD, {"llm": {"enabled": True, "validation": "veto"}}))


def test_the_analyst_and_a_cycle_shorter_than_the_candle_do_not_go_together() -> None:
    """The research is shared per candle close (D-032): between the closes each cycle would
    wait two minutes for peers that do not run, then pay a research of its own (D-059)."""
    analyst = {"llm": {"enabled": True}}
    with pytest.raises(ValidationError, match="cycle menor que o timeframe exige llm"):
        local_profile(**merge(LEAD, {**analyst, "cycle": "10m"}))
    # the candle itself is no shorter cycle (D-054), and neither is none
    assert local_profile(**merge(LEAD, {**analyst, "cycle": "1h"})).cadence == "60m"
    assert local_profile(**merge(LEAD, analyst)).cadence == "1h"
    assert local_profile(**merge(LEAD, {"cycle": "10m"})).cadence == "10m"  # without it: D-053


def test_a_cycle_shorter_than_the_candle_is_only_for_the_lead_traders_consensus() -> None:
    """D-053: the traders' trades arrive between the candle closes, the candles do not."""
    assert local_profile(**LEAD).cadence == "1h"  # without a cycle: at each close
    assert local_profile(**merge(LEAD, {"cycle": "10m"})).cadence == "10m"
    assert local_profile(**merge(LEAD, {"cycle": "30m"})).cycle == timedelta(minutes=30)
    # D-054: the candle itself is a cycle too, the same as none
    assert local_profile(**merge(LEAD, {"cycle": "1h"})).cadence == "60m"
    with pytest.raises(ValidationError, match="cycle é só da família lead_consensus"):
        local_profile(cycle="10m")
    data = raw_config()
    data["profiles"]["moderado"]["cycle"] = "1h"
    with pytest.raises(ValidationError, match="cycle é só da família lead_consensus"):
        StrategyConfig.model_validate(data)


@pytest.mark.parametrize("cycle", ["4m", "7m", "2h", 450])
def test_a_cycle_divides_the_candle_in_whole_minutes_of_at_least_five(cycle: object) -> None:
    """4 minutes is too short, 7 does not divide the hour, 2 hours are longer than the candle
    and 450 seconds (7.5 minutes) is no whole number of minutes."""
    with pytest.raises(ValidationError, match="cycle deve ter minutos inteiros"):
        local_profile(**merge(LEAD, {"cycle": cycle}))


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
    assert selection.rank_by == "score" and selection.min_roi_pct is not None
    assert selection.max_copier_loss_pct is not None
    assert selection.min_sharpe is not None and selection.min_sharpe > 0
    assert selection.min_positive_weeks_pct is not None
    assert selection.min_positive_weeks_pct > 50 and selection.min_recent_gain_pct is not None
    assert selection.max_drawdown_pct is not None and selection.max_drawdown_pct <= 20
    assert LeadsConfig().max_data_age < timedelta(hours=1)  # stale trades are no signal
    assert LOCAL_PULLBACK["family"] != "lead_consensus"  # the fixture stays a pullback
