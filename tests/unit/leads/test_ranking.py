"""Which lead traders are followed (D-052): the figures, the PnL curve, the trading rhythm
and the score."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.support.leads import NOW, RISING, active, stats, trade
from trade_agent.leads.ranking import (
    COPIERS_LOST,
    DEEP_DRAWDOWN,
    FEW_TRADES,
    IDLE,
    INCONSISTENT,
    LOW_ROI,
    LOW_SHARPE,
    SMALL,
    TOO_MANY_TRADES,
    WEAK_RECENT,
    YOUNG,
    activity_reasons,
    assess,
    best,
    candidates,
    figure_reasons,
    positive_weeks,
    recent_gain_pct,
)
from trade_agent.strategy.profiles import LeadSelectionConfig

D = Decimal
CONFIG = LeadSelectionConfig()


def test_a_sound_portfolio_passes_every_figure() -> None:
    assert figure_reasons(stats(), CONFIG) == ()


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"trading_days": 89}, YOUNG),
        ({"aum": D(3999)}, SMALL),  # 999 of its own once the copiers' 3000 are out
        ({"roi_pct": 2.9}, LOW_ROI),
        ({"mdd_pct": 20.1}, DEEP_DRAWDOWN),
        ({"sharpe": 0.99}, LOW_SHARPE),
        ({"sharpe": None}, LOW_SHARPE),
        ({"copier_pnl": D(-151)}, COPIERS_LOST),  # more than 5% of the copiers' 3000
    ],
)
def test_each_figure_refuses_on_its_own(changes: dict[str, Any], reason: str) -> None:
    assert figure_reasons(stats(**changes), CONFIG) == (reason,)


def test_the_cumulative_return_alone_never_qualifies_a_portfolio() -> None:
    # the kind of row a ranking by return shows first: +300% with a 60% drawdown
    lucky = stats(roi_pct=300.0, mdd_pct=60.0, sharpe=0.4, copier_pnl=D(-2000))
    assert figure_reasons(lucky, CONFIG) == (DEEP_DRAWDOWN, LOW_SHARPE, COPIERS_LOST)


def test_the_copiers_loss_has_a_tolerance_and_can_be_left_unchecked() -> None:
    assert figure_reasons(stats(copier_pnl=D(-150)), CONFIG) == ()  # exactly 5%
    alone = stats(copy_aum=D(0), copier_pnl=D(0))  # nobody copies it: nothing was lost
    assert figure_reasons(alone, CONFIG) == ()
    unchecked = CONFIG.model_copy(update={"max_copier_loss_pct": None})
    assert figure_reasons(stats(copier_pnl=D(-2000)), unchecked) == ()


def test_candidates_keep_the_rankings_order_and_count_the_refusals() -> None:
    pool = [
        stats("a"),
        stats("b", roi_pct=1.0, sharpe=0.2),
        stats("c"),
        stats("a", roi_pct=99.0),  # the same portfolio on a later page: its last row counts
        stats("d"),
    ]
    passed, rejected = candidates(pool, CONFIG.model_copy(update={"shortlist": 2, "max_leads": 2}))
    assert [lead.portfolio_id for lead in passed] == ["a", "c"]
    assert passed[0].roi_pct == 99.0
    assert rejected == {LOW_ROI: 1, LOW_SHARPE: 1}


def test_positive_weeks_counts_back_from_the_last_day() -> None:
    assert positive_weeks(RISING) == 1.0
    assert positive_weeks([5.0] * 90) == 0.0
    # 14 days: one step down, one up
    assert positive_weeks([10.0] * 7 + [9.0] + [9.0] * 6 + [12.0]) == 0.5
    assert positive_weeks([1.0, 2.0, 3.0]) is None
    assert positive_weeks([]) is None


def test_the_recent_gain_is_the_pnl_of_30_days_over_the_balance() -> None:
    assert recent_gain_pct(RISING, D(2000)) == pytest.approx(1.5)  # 30 USDT of 2000
    assert recent_gain_pct([100.0] * 60 + [40.0] * 30, D(1000)) == pytest.approx(-6.0)
    assert recent_gain_pct(RISING[:30], D(2000)) is None  # no day 30 days back
    assert recent_gain_pct(RISING, D(0)) is None


def test_a_deposit_does_not_look_like_a_loss() -> None:
    """Why the curve is the PnL and not the ranking's ROI: on 2026-11-19 a portfolio's ROI
    went from +84% to +23% in three days with its PnL unchanged (a deposit diluted it)."""
    pnl = [600.0 + day for day in range(90)]
    assert recent_gain_pct(pnl, D(2800)) == pytest.approx(30 / 2800 * 100)
    assert positive_weeks(pnl) == 1.0


def test_the_trading_rhythm_of_a_person() -> None:
    assert activity_reasons(active(), NOW, CONFIG) == ()
    assert activity_reasons([], NOW, CONFIG) == (IDLE,)
    last_week = [trade(hours_ago=24 * 8 + h) for h in range(10)]
    assert activity_reasons(last_week, NOW, CONFIG) == (IDLE,)
    few = [trade(hours_ago=24), trade(hours_ago=24 * 40), trade(hours_ago=24 * 50)]
    assert activity_reasons(few, NOW, CONFIG) == (FEW_TRADES,)


def test_a_grid_bot_trades_too_often_to_be_a_signal() -> None:
    grid = [trade(hours_ago=1 + minutes / 60) for minutes in range(0, 3000, 30)]  # 100 in 2 days
    assert activity_reasons(grid, NOW, CONFIG) == (TOO_MANY_TRADES,)
    # the span counts as at least one day: 21 trades in an hour are a bot too
    burst = [trade(hours_ago=1 + minute / 60) for minute in range(21)]
    assert activity_reasons(burst, NOW, CONFIG) == (TOO_MANY_TRADES,)
    busy = [trade(hours_ago=1 + 3 * n) for n in range(100)]  # 8 a day
    assert activity_reasons(busy, NOW, CONFIG) == ()


def test_the_score_is_bounded_and_no_extreme_figure_carries_it() -> None:
    sound = assess(stats(), RISING, active(), NOW, CONFIG)
    assert sound.eligible and sound.reasons == ()
    assert sound.consistency == 1.0
    assert sound.recent_gain_pct == pytest.approx(1.5)
    # 0.40 × 1.0 + 0.35 × (12 ÷ 6 ÷ 5) + 0.25 × (1.5 ÷ 10)
    assert sound.score == pytest.approx(0.40 + 0.35 * 0.4 + 0.25 * 0.15)
    # a 300% ROI with no drawdown yet is worth the cap of its part, not more
    extreme = assess(stats(roi_pct=300.0, mdd_pct=0.0), RISING, active(), NOW, CONFIG)
    assert extreme.score == pytest.approx(0.40 + 0.35 + 0.25 * 0.15)
    perfect = assess(stats(roi_pct=300.0, mdd_pct=0.0, aum=D(3100)), RISING, active(), NOW, CONFIG)
    assert perfect.score == 1.0  # 30 USDT on a balance of 100: the recent part at its cap too
    loser = assess(stats(roi_pct=-20.0), [0.0] * 90, active(), NOW, CONFIG)
    assert loser.score == 0.0


def test_the_curve_and_the_trades_refuse_a_candidate() -> None:
    falling = [float(90 - day) for day in range(90)]
    refused = assess(stats(), falling, [], NOW, CONFIG)
    assert refused.reasons == (INCONSISTENT, WEAK_RECENT, IDLE)
    assert not refused.eligible
    short = assess(stats(), RISING[:5], active(), NOW, CONFIG)
    assert short.reasons == (INCONSISTENT, WEAK_RECENT)
    assert short.consistency is None and short.recent_gain_pct is None
    assert short.score == pytest.approx(0.35 * 0.4)
    # six of twelve weeks up is below the 55% of the default
    half = [float(week // 2) for week in range(13) for _ in range(7)][:90]
    assert assess(stats(), half, active(), NOW, CONFIG).reasons[0] == INCONSISTENT


def test_the_best_scores_among_the_eligible_are_followed() -> None:
    config = CONFIG.model_copy(update={"max_leads": 2})
    steady = assess(stats("steady"), RISING, active(), NOW, config)
    strong = assess(stats("strong", roi_pct=24.0, mdd_pct=6.0), RISING, active(), NOW, config)
    modest = assess(stats("modest", roi_pct=4.0), RISING, active(), NOW, config)
    idle = assess(stats("idle", roi_pct=90.0, mdd_pct=2.0), RISING, [], NOW, config)
    assert idle.score > strong.score > steady.score > modest.score
    assert [a.lead.portfolio_id for a in best([modest, idle, steady, strong], config)] == [
        "strong",
        "steady",
    ]
    assert best([idle], config) == []


def test_activity_uses_the_configured_windows() -> None:
    patient = CONFIG.model_copy(update={"max_idle": timedelta(days=30), "min_trades_30d": 1})
    assert activity_reasons([trade(hours_ago=24 * 20)], NOW, patient) == ()
