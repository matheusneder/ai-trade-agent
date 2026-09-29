"""Coleta das leituras de risco (``RiskSnapshot``) a partir do banco e da exchange.

O patrimônio considerado é o **do agente**: capital gerido + PnL realizado + PnL não
realizado das posições abertas (a preço de venda). Saldos da conta que não pertencem ao
agente não entram. A abertura do dia (UTC) e o pico ficam persistidos em ``checkpoints``.
"""

import statistics
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.models import BookTicker
from trade_agent.exchange.rest import CallHealth
from trade_agent.execution.positions import Position
from trade_agent.persistence.store import Store
from trade_agent.reconcile.reconciler import ReconcileReport
from trade_agent.risk.guard import RiskSnapshot
from trade_agent.strategy.profiles import StrategyConfig

log = structlog.get_logger(__name__)

EQUITY_KEY = "risk.equity"
BENCHMARK = "BTCUSDT"
STABLE_PAIRS = ("USDCUSDT", "FDUSDUSDT")
"""Referências de paridade da moeda de cotação (USDT) contra outros dólares digitais."""


def _mid(book: BookTicker) -> Decimal:
    return (book.bid_price + book.ask_price) / 2


def consecutive_losses(closed: Sequence[Position]) -> int:
    """Perdas seguidas a partir da posição encerrada mais recente."""
    streak = 0
    for position in closed:
        if position.realized_pnl is None or position.realized_pnl >= 0:
            break
        streak += 1
    return streak


def quote_deviation(books: dict[str, BookTicker]) -> float | None:
    """Desvio absoluto do USDT: 1 / mediana(USDC/USDT, FDUSD/USDT) − 1."""
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

    def note_reconcile(self, report: ReconcileReport) -> None:
        """Órfãs e erros da última reconciliação alimentam ``reconcile_mismatch``."""
        self.reconcile_anomalies = len(report.orphans) + len(report.errors)

    async def _btc_change_1h(self) -> float | None:
        candles = await self._api.klines(BENCHMARK, "1m", limit=61)
        if len(candles) < 61:
            return None
        first, last = Decimal(str(candles[0][4])), Decimal(str(candles[-1][4]))
        return float(last / first - 1) if first > 0 else None

    async def _equity_marks(self, equity: Decimal, now: datetime) -> tuple[Decimal, Decimal]:
        """Abertura do dia (UTC) e pico do patrimônio, atualizados e persistidos."""
        day = now.date().isoformat()
        saved = await self._store.get_checkpoint(EQUITY_KEY) or {}
        day_start = Decimal(saved["day_start"]) if saved.get("day") == day else equity
        peak = max(Decimal(saved.get("peak", equity)), equity)
        await self._store.set_checkpoint(
            EQUITY_KEY, {"day": day, "day_start": str(day_start), "peak": str(peak)}
        )
        return day_start, peak

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
        day_start, peak = await self._equity_marks(equity, now)
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
