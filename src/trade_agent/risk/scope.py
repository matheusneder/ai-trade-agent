"""Entry pauses of a pair-local profile (D-044), from its own equity and closed trades.

Pure and dependency-free, like :mod:`trade_agent.signals`: the lab strategy imports it inside
the Freqtrade image. The production Risk Guard does not run it yet, which is why
:class:`~trade_agent.strategy.profiles.StrategyConfig` refuses an enabled pair-local profile.

* daily loss: the marked-to-market equity ``daily_loss_pct`` (% of the capital) below its
  first mark of the UTC day pauses new entries for ``cooldown``, once a day;
* consecutive losses: ``consecutive_losses`` losing trades in a row pause them for
  ``cooldown`` (the count restarts);
* drawdown: ``max_drawdown_pct`` below the equity's peak stops new entries until a review,
  with no expiry. Unlike the global ``halt``, the protection and the technical exits go on;
* re-entry: no new entry on a pair for ``reentry`` after a stop on it.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

DAILY_LOSS = "perda diária do perfil"
LOSING_STREAK = "perdas seguidas do perfil"
DRAWDOWN = "drawdown do perfil: novas entradas só após revisão"
REENTRY = "reentrada após stop no par"


@dataclass(frozen=True, slots=True)
class ScopeLimits:
    capital: float
    daily_loss_pct: float | None = None
    consecutive_losses: int | None = None
    cooldown: timedelta = timedelta(hours=24)
    max_drawdown_pct: float | None = None
    reentry: timedelta = timedelta(0)


@dataclass
class ScopeRisk:
    limits: ScopeLimits
    peak: float | None = None
    day: date | None = None
    day_start: float = 0.0
    daily_hit: bool = False
    """Today's daily-loss trigger has acted."""
    losses: int = 0
    paused_until: datetime | None = None
    pause_reason: str = ""
    review: bool = False
    """The drawdown trigger acted: no new entries until a review."""
    stops: dict[str, datetime] = field(default_factory=dict)
    events: list[tuple[datetime, str]] = field(default_factory=list)

    def mark(self, at: datetime, equity: float) -> None:
        """A marked-to-market equity reading."""
        limits = self.limits
        if self.day != at.date():
            self.day, self.day_start, self.daily_hit = at.date(), equity, False
        self.peak = equity if self.peak is None else max(self.peak, equity)
        daily, drawdown = limits.daily_loss_pct, limits.max_drawdown_pct
        lost_today = self.day_start - equity
        if daily is not None and not self.daily_hit and lost_today >= limits.capital * daily / 100:
            self.daily_hit = True
            self._pause(at, DAILY_LOSS)
        fell = self.peak - equity
        if drawdown is not None and not self.review and fell >= self.peak * drawdown / 100:
            self.review = True
            self.events.append((at, DRAWDOWN))

    def closed(self, at: datetime, symbol: str, pnl: float, *, stop: bool) -> None:
        """A closed trade: its result, and whether its stop closed it."""
        if stop:
            self.stops[symbol] = at
        self.losses = self.losses + 1 if pnl < 0 else 0
        streak = self.limits.consecutive_losses
        if streak is not None and self.losses >= streak:
            self.losses = 0
            self._pause(at, LOSING_STREAK)

    def _pause(self, at: datetime, reason: str) -> None:
        until = at + self.limits.cooldown
        if self.paused_until is None or until > self.paused_until:
            self.paused_until, self.pause_reason = until, reason
        self.events.append((at, reason))

    def blocked(self, at: datetime, symbol: str) -> str | None:
        """Why a new entry on ``symbol`` is not allowed at ``at`` (``None``: it is)."""
        if self.review:
            return DRAWDOWN
        if self.paused_until is not None and at < self.paused_until:
            return self.pause_reason
        stop = self.stops.get(symbol)
        if stop is not None and at < stop + self.limits.reentry:
            return REENTRY
        return None
