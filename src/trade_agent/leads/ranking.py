"""Which lead traders to follow (pure functions over the ranking's figures, the PnL curve and
the trades).

A ranking sorted by return rewards whoever took the most risk and has not blown up yet, so
no criterion here is the cumulative return alone (D-052). The choice has two steps, both
with the thresholds of the profile's ``leads.selection``:

1. :func:`candidates`: the portfolios whose **ranking figures** pass (age, the owner's own
   money, ROI, drawdown, Sharpe ratio, what the copiers lost), in the ranking's order;
2. :func:`assess`, for each candidate, with two more readings: its **PnL curve** (gains
   spread over the weeks, and a recent gain) and its **trades** (a human trading rhythm).

The eligible ones are ordered by a **score** in [0, 1] of three bounded parts:

=====================  ======  ======================================================
consistency            0.40    share of the 7-day steps of the PnL curve that rose
risk-adjusted return   0.35    ROI ÷ drawdown, the drawdown floored and the ratio capped
recent performance     0.25    PnL of the last 30 days, in % of the balance, capped
=====================  ======  ======================================================

The floors and caps keep one extreme figure from carrying the score: a drawdown of zero
(nothing sold at a loss *yet*) does not make the ratio infinite, and a 300% ROI is worth no
more than the cap. The rejection reasons are operator text (Portuguese).

A profile that only tests the copy trading flow (D-054) leaves the performance criteria out
(``null``) and orders the eligible ones by their trades per day (``rank_by: activity``): it
follows whoever trades most often, whatever the results.
"""

import bisect
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trade_agent.leads.source import LeadStats, LeadTrade, PnlPoint
from trade_agent.strategy.profiles import LeadSelectionConfig

WEEK = timedelta(days=7)
RECENT = timedelta(days=30)
ACTIVITY_WINDOW = timedelta(days=30)
MDD_FLOOR = 2.0
"""Drawdown (%) below which the risk-adjusted return stops improving."""
RATIO_CAP = 5.0
RECENT_CAP = 10.0
"""Gain of the last 30 days (% of the balance) worth the whole recent-performance part."""
WEIGHTS = (0.40, 0.35, 0.25)

YOUNG = "histórico curto"
SMALL = "saldo próprio baixo"
LOW_ROI = "ROI abaixo do mínimo"
DEEP_DRAWDOWN = "drawdown acima do limite"
LOW_SHARPE = "Sharpe ausente ou abaixo do mínimo"
COPIERS_LOST = "copiadores com perda acima do limite"
INCONSISTENT = "semanas positivas abaixo do mínimo"
WEAK_RECENT = "desempenho recente abaixo do mínimo"
IDLE = "sem operações recentes"
FEW_TRADES = "poucas operações em 30 dias"
TOO_MANY_TRADES = "operações demais (provável robô de grade)"


def figure_reasons(lead: LeadStats, config: LeadSelectionConfig) -> tuple[str, ...]:
    """What the portfolio's ranking figures fail; a ``null`` criterion is not checked."""
    roi, drawdown, sharpe = config.min_roi_pct, config.max_drawdown_pct, config.min_sharpe
    copier_loss = config.max_copier_loss_pct
    checks = [
        (lead.trading_days < config.min_days, YOUNG),
        (lead.balance < config.min_balance, SMALL),
        (roi is not None and lead.roi_pct < roi, LOW_ROI),
        (drawdown is not None and lead.mdd_pct > drawdown, DEEP_DRAWDOWN),
        (sharpe is not None and (lead.sharpe is None or lead.sharpe < sharpe), LOW_SHARPE),
        (
            copier_loss is not None
            and -lead.copier_pnl * 100 > Decimal(str(copier_loss)) * lead.copy_aum,
            COPIERS_LOST,
        ),
    ]
    return tuple(reason for failed, reason in checks if failed)


def candidates(
    pool: Iterable[LeadStats], config: LeadSelectionConfig
) -> tuple[list[LeadStats], Counter[str]]:
    """The portfolios of the pool worth two more readings, in the ranking's order (best
    Sharpe ratio first), and how many each criterion refused: the first ``shortlist``, or
    with ``rank_by: activity`` all of them, since the ranking has no trade count and its
    first portfolios may all be idle; the tracker then reads them in order until it has
    found ``shortlist`` eligible ones (D-054)."""
    passed: list[LeadStats] = []
    rejected: Counter[str] = Counter()
    for lead in {s.portfolio_id: s for s in pool}.values():
        reasons = figure_reasons(lead, config)
        rejected.update(reasons)
        if not reasons:
            passed.append(lead)
    return (passed if config.rank_by == "activity" else passed[: config.shortlist]), rejected


def pnl_at(curve: Sequence[PnlPoint], moment: datetime) -> float | None:
    """The cumulative PnL at ``moment``: its last point at or before it (a missing day keeps
    the day before); ``None`` before the curve starts."""
    index = bisect.bisect_right([point.at for point in curve], moment)
    return curve[index - 1].pnl if index else None


def positive_weeks(curve: Sequence[PnlPoint]) -> float | None:
    """Share (0–1) of the 7-day steps of the cumulative PnL curve that ended higher, counted
    back from its last point by the calendar; ``None`` without a whole step."""
    if not curve:
        return None
    end, steps = curve[-1].at, []
    while (earlier := pnl_at(curve, end - WEEK)) is not None:
        later = pnl_at(curve, end)
        assert later is not None  # noqa: S101 - end is after earlier, inside the curve
        steps.append(later - earlier)
        end -= WEEK
    return sum(step > 0 for step in steps) / len(steps) if steps else None


def recent_gain_pct(curve: Sequence[PnlPoint], balance: Decimal) -> float | None:
    """PnL of the last 30 calendar days of the curve, in % of the trader's own balance;
    ``None`` on a shorter curve or without a balance."""
    start = pnl_at(curve, curve[-1].at - RECENT) if curve else None
    if start is None or balance <= 0:
        return None
    return (curve[-1].pnl - start) / float(balance) * 100


def activity_reasons(
    trades: Sequence[LeadTrade], now: datetime, config: LeadSelectionConfig
) -> tuple[str, ...]:
    """What the portfolio's trading rhythm fails; ``trades`` are its most recent ones."""
    if not trades or now - max(t.at for t in trades) > config.max_idle:
        return (IDLE,)
    recent = [t for t in trades if now - t.at <= ACTIVITY_WINDOW]
    reasons = []
    if len(recent) < config.min_trades_30d:
        reasons.append(FEW_TRADES)
    days = max((max(t.at for t in trades) - min(t.at for t in trades)) / timedelta(days=1), 1.0)
    if len(trades) / days > config.max_trades_per_day:
        reasons.append(TOO_MANY_TRADES)
    return tuple(reasons)


def trades_per_day(trades: Sequence[LeadTrade], now: datetime) -> float:
    """How often the portfolio trades: its most recent ``trades`` over the days from the
    oldest of them to ``now`` (at least one), so a pause since the last one lowers it."""
    if not trades:
        return 0.0
    return len(trades) / max((now - min(t.at for t in trades)) / timedelta(days=1), 1.0)


@dataclass(frozen=True, slots=True)
class Assessment:
    lead: LeadStats
    score: float
    consistency: float | None
    recent_gain_pct: float | None
    reasons: tuple[str, ...]
    """Why the candidate is not eligible; empty when it is."""
    trades_per_day: float = 0.0

    @property
    def eligible(self) -> bool:
        return not self.reasons


def assess(
    lead: LeadStats,
    curve: Sequence[PnlPoint],
    trades: Sequence[LeadTrade],
    now: datetime,
    config: LeadSelectionConfig,
) -> Assessment:
    """A candidate's score and what its PnL curve and its trades fail, if anything."""
    consistency, recent = positive_weeks(curve), recent_gain_pct(curve, lead.balance)
    weeks, gain = config.min_positive_weeks_pct, config.min_recent_gain_pct
    checks = [
        (weeks is not None and (consistency is None or consistency * 100 < weeks), INCONSISTENT),
        (gain is not None and (recent is None or recent < gain), WEAK_RECENT),
    ]
    ratio = min(max(lead.roi_pct, 0.0) / max(lead.mdd_pct, MDD_FLOOR), RATIO_CAP) / RATIO_CAP
    recent_part = min(max(recent or 0.0, 0.0) / RECENT_CAP, 1.0)
    parts = (consistency or 0.0, ratio, recent_part)
    return Assessment(
        lead=lead,
        score=round(sum(w * part for w, part in zip(WEIGHTS, parts, strict=True)), 4),
        consistency=consistency,
        recent_gain_pct=recent,
        reasons=(
            *(reason for failed, reason in checks if failed),
            *activity_reasons(trades, now, config),
        ),
        trades_per_day=trades_per_day(trades, now),
    )


def best(assessments: Iterable[Assessment], config: LeadSelectionConfig) -> list[Assessment]:
    """The eligible candidates to follow: the ``max_leads`` best scores, or with ``rank_by:
    activity`` the ones that trade most often (D-054)."""

    def key(a: Assessment) -> tuple[float, ...]:
        return (a.trades_per_day, a.score) if config.rank_by == "activity" else (a.score,)

    eligible = sorted((a for a in assessments if a.eligible), key=key, reverse=True)
    return eligible[: config.max_leads]
