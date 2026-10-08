"""Collection of the risk readings (``RiskSnapshot``) from the database and the exchange.

The equity considered is **the agent's**: managed capital + realized PnL + unrealized PnL
of the open positions (at the sell price). Account balances that do not belong to the
agent are left out. The day's open (UTC) and the peak are persisted in ``checkpoints``,
together with the managed capital in force when they were recorded.
"""

import statistics
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from trade_agent import tracing
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.models import BookTicker
from trade_agent.exchange.rest import CallHealth
from trade_agent.execution.positions import Position
from trade_agent.persistence.store import Store
from trade_agent.reconcile.reconciler import ReconcileReport
from trade_agent.risk.guard import RiskSnapshot
from trade_agent.strategy.profiles import StrategyConfig

RECONCILE_FAILURES_TO_COUNT = 2
"""Consecutive reconciliations with errors from which the errors count in reconcile_mismatch."""

log = structlog.get_logger(__name__)

EQUITY_KEY = "risk.equity"
BENCHMARK = "BTCUSDT"
STABLE_PAIRS = ("USDCUSDT", "FDUSDUSDT")
"""Parity references of the quote asset (USDT) against other digital dollars."""


def _mid(book: BookTicker) -> Decimal:
    return (book.bid_price + book.ask_price) / 2


def consecutive_losses(closed: Sequence[Position]) -> int:
    """Consecutive losses starting from the most recently closed position."""
    streak = 0
    for position in closed:
        if position.realized_pnl is None or position.realized_pnl >= 0:
            break
        streak += 1
    return streak


def quote_deviation(books: dict[str, BookTicker]) -> float | None:
    """Absolute deviation of USDT: 1 / median(USDC/USDT, FDUSD/USDT) − 1."""
    mids = [_mid(books[s]) for s in STABLE_PAIRS if s in books and _mid(books[s]) > 0]
    if not mids:
        return None
    return abs(float(1 / statistics.median(mids)) - 1)


class RiskMonitor:
    def __init__(
        self,
        *,
        api: BinanceSpotApi,
        store: Store,
        strategy: StrategyConfig,
        health: CallHealth,
        fear_greed: Callable[[], int | None] = lambda: None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api = api
        self._store = store
        self._strategy = strategy
        self._health = health
        self._fear_greed = fear_greed
        self._clock = clock
        self._names = {p.code: name for name, p in strategy.profiles.items()}
        self.reconcile_anomalies = 0
        self._failed_in_a_row = 0

    def note_reconcile(self, report: ReconcileReport) -> None:
        """Orphans and persistent errors of the last reconciliation feed ``reconcile_mismatch``.

        An orphan (an agent list on Binance without a position) is a real mismatch and counts
        right away. An error is a check that did not finish (network, DNS, Binance down) and
        only counts if the following reconciliations fail too: on 2026-10-07, 2 min without
        DNS turned into a pause with no deadline, even though the reconciliation 5 min later
        came out clean. A long API outage already pauses through api_error_rate_5m.
        """
        self._failed_in_a_row = self._failed_in_a_row + 1 if report.errors else 0
        persistent = self._failed_in_a_row >= RECONCILE_FAILURES_TO_COUNT
        self.reconcile_anomalies = len(report.orphans) + (len(report.errors) if persistent else 0)

    async def _btc_change_1h(self) -> float | None:
        candles = await self._api.klines(BENCHMARK, "1m", limit=61)
        if len(candles) < 61:
            return None
        first, last = Decimal(str(candles[0][4])), Decimal(str(candles[-1][4]))
        return float(last / first - 1) if first > 0 else None

    async def _equity_marks(
        self, equity: Decimal, baseline: Decimal, now: datetime
    ) -> tuple[Decimal, Decimal]:
        """The day's open (UTC) and the equity peak, updated and persisted.

        Changing ``managed_capital`` is neither a gain nor a loss: the marks follow the change
        in capital, and only the trading result counts for the daily loss and the drawdown.
        Marks recorded without the reference capital start again from the current equity.
        """
        day = now.date().isoformat()
        saved = await self._store.get_checkpoint(EQUITY_KEY) or {}
        same_day = saved.get("day") == day
        day_start = Decimal(saved["day_start"]) if same_day else equity
        peak = Decimal(saved.get("peak", equity))
        previous = saved.get("baseline")
        if saved and (previous is None or Decimal(previous) != baseline):
            if previous is None:
                day_start = peak = equity
            else:
                shift = baseline - Decimal(previous)
                peak += shift
                if same_day:
                    day_start += shift
            log.warning(
                "risk.equity_rebased",
                previous_baseline=previous,
                baseline=str(baseline),
                day_start=str(day_start),
                peak=str(peak),
            )
        peak = max(peak, equity)
        await self._store.set_checkpoint(
            EQUITY_KEY,
            {"day": day, "day_start": str(day_start), "peak": str(peak), "baseline": str(baseline)},
        )
        return day_start, peak

    @tracing.traced("risk", "risk.snapshot")
    async def snapshot(self) -> RiskSnapshot:
        now = self._clock()
        active = await self._store.active_positions()
        closed = await self._store.closed_positions()
        wanted = {p.symbol for p in active} | {BENCHMARK, *STABLE_PAIRS}
        books = {b.symbol: b for b in await self._api.book_tickers() if b.symbol in wanted}
        unrealized = sum(
            (
                (books[p.symbol].bid_price - p.entry_price) * (p.protected_qty or p.entry_qty or 0)
                for p in active
                if p.entry_price is not None and p.symbol in books
            ),
            Decimal(0),
        )
        baseline = self._strategy.account.managed_capital
        realized = await self._store.realized_pnl_total()
        equity = baseline + realized + unrealized
        day_start, peak = await self._equity_marks(equity, baseline, now)
        exposure = sum(
            (p.entry_quote or p.planned_qty * p.planned_price for p in active), Decimal(0)
        )

        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily: dict[str, Decimal] = {}
        streaks: dict[str, int] = {}
        for code, name in self._names.items():
            own = [p for p in closed if p.profile == code]
            streaks[name] = consecutive_losses(own)
            daily[name] = sum(
                (
                    p.realized_pnl or Decimal(0)
                    for p in own
                    if p.closed_at and p.closed_at >= midnight
                ),
                Decimal(0),
            )
        snapshot = RiskSnapshot(
            now=now,
            equity=equity,
            day_start_equity=day_start,
            peak_equity=peak,
            baseline_equity=baseline,
            consecutive_losses=consecutive_losses(closed),
            profile_consecutive_losses=streaks,
            profile_daily_pnl=daily,
            profile_capital={
                name: self._strategy.profile_capital(name) for name in self._strategy.profiles
            },
            btc_change_1h=await self._btc_change_1h(),
            quote_deviation=quote_deviation(books),
            fear_greed=self._fear_greed(),
            api_error_rate=self._health.error_rate(),
            reconcile_anomalies=self.reconcile_anomalies,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            exposure=exposure,
            active_positions=len(active),
        )
        tracing.annotate(
            equity=equity,
            day_start=day_start,
            peak=peak,
            positions=len(active),
            api_error_rate=snapshot.api_error_rate,
        )
        log.debug(
            "risk.snapshot",
            equity=str(equity),
            day_start=str(day_start),
            peak=str(peak),
            realized=str(realized),
            unrealized=str(unrealized),
            exposure=str(exposure),
            positions=len(active),
            consecutive_losses=snapshot.consecutive_losses,
            btc_change_1h=snapshot.btc_change_1h,
            quote_deviation=snapshot.quote_deviation,
            fear_greed=snapshot.fear_greed,
            api_error_rate=snapshot.api_error_rate,
            reconcile_anomalies=snapshot.reconcile_anomalies,
        )
        return snapshot
