"""Entry pauses of a pair-local profile (D-044): daily loss, losing streak, drawdown, re-entry."""

from datetime import UTC, datetime, timedelta

from trade_agent.risk.scope import (
    DAILY_LOSS,
    DRAWDOWN,
    LOSING_STREAK,
    REENTRY,
    ScopeLimits,
    ScopeRisk,
)

T0 = datetime(2026, 10, 20, tzinfo=UTC)
H = timedelta(hours=1)


def test_the_daily_loss_counts_open_positions_and_pauses_once_a_day() -> None:
    scope = ScopeRisk(ScopeLimits(capital=100, daily_loss_pct=1.5, cooldown=24 * H))
    scope.mark(T0, 100.0)
    scope.mark(T0 + H, 98.6)  # -1.4: below the limit
    assert scope.blocked(T0 + H, "SOLUSDT") is None
    scope.mark(T0 + 2 * H, 98.5)  # -1.5 marked to market
    assert scope.blocked(T0 + 3 * H, "SOLUSDT") == DAILY_LOSS
    scope.mark(T0 + 4 * H, 97.0)  # the same day: acts once
    assert scope.events == [(T0 + 2 * H, DAILY_LOSS)]
    assert scope.blocked(T0 + 26 * H, "SOLUSDT") is None  # the cooldown expired
    scope.mark(T0 + 26 * H, 97.0)  # a new day: its own first mark
    assert scope.day_start == 97.0 and not scope.daily_hit


def test_a_losing_streak_pauses_and_starts_counting_again() -> None:
    scope = ScopeRisk(ScopeLimits(capital=100, consecutive_losses=3, cooldown=24 * H))
    for k, pnl in enumerate((-1.0, -1.0, 2.0, -1.0, -1.0)):
        scope.closed(T0 + k * H, "SOLUSDT", pnl, stop=False)
    assert scope.blocked(T0 + 5 * H, "ETHUSDT") is None  # the win reset the count
    scope.closed(T0 + 5 * H, "ETHUSDT", -0.5, stop=False)
    assert scope.blocked(T0 + 6 * H, "ETHUSDT") == LOSING_STREAK
    assert scope.losses == 0
    # a shorter pause never shortens a longer one already running
    scope.paused_until = T0 + 100 * H
    scope.closed(T0 + 7 * H, "ETHUSDT", -1.0, stop=False)
    scope.closed(T0 + 8 * H, "ETHUSDT", -1.0, stop=False)
    scope.closed(T0 + 9 * H, "ETHUSDT", -1.0, stop=False)
    assert scope.paused_until == T0 + 100 * H


def test_the_drawdown_stops_new_entries_until_a_review() -> None:
    scope = ScopeRisk(ScopeLimits(capital=100, max_drawdown_pct=8))
    scope.mark(T0, 100.0)
    scope.mark(T0 + H, 110.0)  # new peak
    scope.mark(T0 + 2 * H, 101.3)  # -7.9% from the peak
    assert scope.blocked(T0 + 2 * H, "SOLUSDT") is None
    scope.mark(T0 + 3 * H, 101.0)  # -8.2%
    assert scope.blocked(T0 + 3 * H, "SOLUSDT") == DRAWDOWN
    scope.mark(T0 + 1000 * H, 130.0)  # no expiry, not even on a recovery
    assert scope.blocked(T0 + 1000 * H, "SOLUSDT") == DRAWDOWN
    assert scope.events == [(T0 + 3 * H, DRAWDOWN)]


def test_no_reentry_on_a_pair_right_after_its_stop() -> None:
    scope = ScopeRisk(ScopeLimits(capital=100, reentry=3 * H))
    scope.closed(T0, "SOLUSDT", -0.5, stop=True)
    scope.closed(T0, "ETHUSDT", -0.5, stop=False)  # a technical exit: no cooldown
    assert scope.blocked(T0 + 2 * H, "SOLUSDT") == REENTRY
    assert scope.blocked(T0 + 2 * H, "ETHUSDT") is None
    assert scope.blocked(T0 + 3 * H, "SOLUSDT") is None


def test_without_limits_nothing_pauses() -> None:
    scope = ScopeRisk(ScopeLimits(capital=100))
    scope.mark(T0, 100.0)
    scope.mark(T0 + H, 1.0)
    for _ in range(10):
        scope.closed(T0 + H, "SOLUSDT", -1.0, stop=True)
    assert scope.blocked(T0 + H, "SOLUSDT") is None
    assert scope.events == []
