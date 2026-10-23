"""Replays the Demo positions' exits on the production tape (``api.binance.com``).

The Binance Spot Demo replays the public taker flow trade by trade against its own copy of
the book. In fast moves one side of that copy goes hollow for under a second, and the Demo
prints prices the real market never had (up to 13% away on 2026-10-10). Those prints arm and
fire the OCO legs. Replaying the same legs on the public ``aggTrades`` tape estimates what
the position would have made in production (``doc/runbook.md``, section 4).

Model (on the Demo tape, the same replay reproduced the 44 Demo exits of 2026-10-01..22 to
the second):

* the entry is the Demo's (same price, quantity and time);
* each confirmed protection list (the entry OPOCO, then each OCO) works from its intent until
  the next one. The legs follow Binance's trailing stop FAQ: tracking starts at
  ``stopPrice`` (at the first trade without one), and the trigger is a ``trailingDelta``
  drop from the highest trade since then;
* a market leg fills at the trigger trade when that trade printed at the bid, else at the
  next trade that did (the best bid, as far as the tape shows it). ``worst`` is the lowest
  trade in the 250 ms after the trigger;
* a ``LIMIT_MAKER`` leg fills at its price once a trade prints above it;
* a Demo exit that was not a leg (decision, fail-safe, manual) is a market sell at the same
  moment, unless a leg fires first;
* when the tape does not confirm a Demo exit, the last legs keep working until the horizon,
  without the later adjustments the agent would have made.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.models import AggTrade
from trade_agent.execution.positions import Position
from trade_agent.persistence.models import ExchangeOrderRecord, FillRecord
from trade_agent.persistence.store import Intent, IntentStatus, Store

BIPS = Decimal(10_000)
MINUTE_MS = 60_000
WORST_WINDOW_MS = 250
"""Window of the pessimistic price after a market leg triggers."""
AGG_TRADES_LIMIT = 1000
KLINES_LIMIT = 1000
CONFIRM_TOLERANCE_MS = MINUTE_MS
CONFIRM_PRICE_TOLERANCE = Decimal("0.01")
"""A replayed exit by the same leg, within a minute of the Demo exit and within 1% of its
fill (about one trailing delta), confirms it."""
MARKET = "MARKET"
"""Reason of an exit that was not an OCO leg."""


class LegKind(StrEnum):
    """Sell legs of the protection lists the agent builds (``execution.orders``)."""

    TAKE_PROFIT = "TAKE_PROFIT"
    LIMIT_MAKER = "LIMIT_MAKER"
    STOP_LOSS = "STOP_LOSS"


@dataclass(frozen=True, slots=True)
class ExitLeg:
    kind: LegKind
    price: Decimal | None
    """``stopPrice`` (activation of a trailing leg) or the ``LIMIT_MAKER`` price; ``None``
    only for a trailing leg that tracks from the first trade."""
    trailing_bips: int | None = None

    def __post_init__(self) -> None:
        if self.price is None and self.trailing_bips is None:
            raise ValueError(f"{self.kind} leg without a price needs a trailing delta")
        if self.kind is LegKind.LIMIT_MAKER and self.trailing_bips is not None:
            raise ValueError("a LIMIT_MAKER leg cannot trail")


@dataclass(frozen=True, slots=True)
class Segment:
    """Protection list working from ``start_ms`` until the next segment."""

    start_ms: int
    legs: tuple[ExitLeg, ...]


@dataclass(frozen=True, slots=True)
class Candle:
    open_ms: int
    high: Decimal
    low: Decimal


@dataclass(frozen=True, slots=True)
class Exit:
    reason: str
    """``LegKind`` value of the leg that fired, or ``MARKET``."""
    time_ms: int
    trigger: Decimal | None
    """Trigger price of a stop leg (``None`` for a limit or market exit)."""
    price: Decimal
    worst: Decimal


class Tape(Protocol):
    """Public market data of one Binance environment."""

    async def candles(self, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
        """1m candles whose open is in ``[start_ms - start_ms % 1m, end_ms]``."""
        ...

    async def trades(self, symbol: str, start_ms: int, end_ms: int) -> list[AggTrade]:
        """Every trade in ``[start_ms, end_ms]`` (at most one hour), oldest first."""
        ...


class BinanceTape:
    """:class:`Tape` over the public REST endpoints (no keys)."""

    def __init__(self, api: BinanceSpotApi) -> None:
        self.api = api

    async def candles(self, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
        candles: list[Candle] = []
        cursor = start_ms - start_ms % MINUTE_MS
        while cursor <= end_ms:
            page = await self.api.klines(
                symbol, "1m", limit=KLINES_LIMIT, start_time=cursor, end_time=end_ms
            )
            if not page:
                break
            candles += [Candle(int(k[0]), Decimal(k[2]), Decimal(k[3])) for k in page]
            cursor = int(page[-1][0]) + MINUTE_MS
        return candles

    async def trades(self, symbol: str, start_ms: int, end_ms: int) -> list[AggTrade]:
        page = await self.api.agg_trades(symbol, start_time=start_ms, end_time=end_ms)
        trades = list(page)
        while len(page) == AGG_TRADES_LIMIT:
            page = [
                t
                for t in await self.api.agg_trades(symbol, from_id=trades[-1].id + 1)
                if t.time <= end_ms
            ]
            trades += page
        return trades


class _LegState:
    """A leg working on the tape (trailing peak included)."""

    def __init__(self, leg: ExitLeg) -> None:
        self.leg = leg
        self.delta = Decimal(leg.trailing_bips or 0) / BIPS
        self.peak: Decimal | None = None
        """Highest trade since the trailing tracking started (``None``: not tracking)."""

    def _reached(self, price: Decimal) -> bool:
        """``stopPrice`` condition of the leg (always true without a price)."""
        if self.leg.price is None:
            return True
        if self.leg.kind is LegKind.STOP_LOSS:
            return price <= self.leg.price
        if self.leg.kind is LegKind.LIMIT_MAKER:
            return price > self.leg.price
        return price >= self.leg.price

    def on_trade(self, price: Decimal) -> Decimal | None:
        """Feeds one trade; returns the trigger price when the trade fires the leg."""
        if self.leg.trailing_bips is None:
            return self.leg.price if self._reached(price) else None
        if self.peak is None:
            if self._reached(price):
                self.peak = price
            return None
        self.peak = max(self.peak, price)
        level = self.peak * (1 - self.delta)
        return level if price <= level else None

    def quiet(self, candle: Candle) -> bool:
        """True if no trade inside ``candle`` can fire the leg or start its tracking."""
        if self.peak is not None:
            return candle.low > max(self.peak, candle.high) * (1 - self.delta)
        # the condition is monotonic in the price: the candle's extremes decide it
        return not (self._reached(candle.low) or self._reached(candle.high))

    def absorb(self, candle: Candle) -> None:
        """Applies a quiet candle: only the trailing peak can move."""
        if self.peak is not None:
            self.peak = max(self.peak, candle.high)


def _market_fill(trades: Sequence[AggTrade], index: int) -> tuple[Decimal, Decimal]:
    """(price, worst) of a market sell sent at ``trades[index]``."""
    first = trades[index]
    later = trades[index:]
    price = next((t.price for t in later if t.buyer_is_maker), first.price)
    worst = min(t.price for t in later if t.time <= first.time + WORST_WINDOW_MS)
    return price, worst


async def _replay_segment(tape: Tape, symbol: str, segment: Segment, end_ms: int) -> Exit | None:
    states = [_LegState(leg) for leg in segment.legs]
    for candle in await tape.candles(symbol, segment.start_ms, end_ms):
        if all(s.quiet(candle) for s in states):
            for s in states:
                s.absorb(candle)
            continue
        start = max(segment.start_ms, candle.open_ms)
        trades = await tape.trades(symbol, start, min(candle.open_ms + MINUTE_MS - 1, end_ms))
        for index, trade in enumerate(trades):
            for s in states:
                level = s.on_trade(trade.price)
                if level is None:
                    continue
                if s.leg.kind is LegKind.LIMIT_MAKER:
                    return Exit(s.leg.kind.value, trade.time, None, level, level)
                price, worst = _market_fill(trades, index)
                return Exit(s.leg.kind.value, trade.time, level, price, worst)
    return None


async def replay(
    tape: Tape,
    symbol: str,
    segments: Sequence[Segment],
    *,
    until_ms: int,
    market_exit_ms: int | None = None,
) -> Exit | None:
    """First exit on ``tape``: a leg of the segment in force, else the market exit.

    Without ``market_exit_ms``, ``None`` means no leg fired until ``until_ms``.
    """
    end = until_ms if market_exit_ms is None else min(until_ms, market_exit_ms)
    for index, segment in enumerate(segments):
        if segment.start_ms > end:
            break
        following = segments[index + 1].start_ms if index + 1 < len(segments) else end
        found = await _replay_segment(tape, symbol, segment, min(following, end))
        if found is not None:
            return found
    if market_exit_ms is None:
        return None
    trades = await tape.trades(symbol, market_exit_ms, market_exit_ms + MINUTE_MS)
    if not trades:
        return None
    price, worst = _market_fill(trades, 0)
    return Exit(MARKET, trades[0].time, None, price, worst)


# ---------------------------------------------------------------------- positions
_LEG_PREFIXES = {"opoco": ("pendingAbove", "pendingBelow"), "oco": ("above", "below")}


def _leg(payload: dict[str, Any], prefix: str) -> ExitLeg:
    kind = LegKind(payload[f"{prefix}Type"])
    price = payload.get(f"{prefix}Price" if kind is LegKind.LIMIT_MAKER else f"{prefix}StopPrice")
    trailing = payload.get(f"{prefix}TrailingDelta")
    return ExitLeg(
        kind,
        Decimal(price) if price is not None else None,
        int(trailing) if trailing is not None else None,
    )


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def segments_from(intents: Sequence[Intent], entry_ms: int) -> list[Segment]:
    """Protection lists of a position, from its confirmed OPOCO/OCO intents.

    The entry OPOCO arms at the entry fill; a later OCO works from its intent.
    """
    segments = []
    for intent in intents:
        prefixes = _LEG_PREFIXES.get(intent.endpoint)
        if intent.status is not IntentStatus.CONFIRMED or prefixes is None:
            continue
        start = entry_ms if intent.endpoint == "opoco" else _ms(intent.created_at)
        segments.append(Segment(start, tuple(_leg(intent.payload, p) for p in prefixes)))
    return segments


@dataclass(frozen=True, slots=True)
class DemoExit:
    reason: str
    time_ms: int
    price: Decimal
    qty: Decimal


def demo_exit(orders: Sequence[ExchangeOrderRecord], fills: Sequence[FillRecord]) -> DemoExit:
    """The position's exit on the Demo: its sell fills and the type of the first sell order."""
    sells = sorted((f for f in fills if not f.is_buyer), key=lambda f: f.trade_time)
    if not sells:
        raise ValueError("position without sell fills")
    order_type = next(o.type for o in orders if o.order_id == sells[0].order_id)
    qty = sum((f.qty for f in sells), Decimal(0))
    quote = sum((f.quote_qty for f in sells), Decimal(0))
    reason = order_type if order_type in LegKind.__members__ else MARKET
    return DemoExit(reason, sells[0].trade_time, quote / qty, qty)


@dataclass(frozen=True, slots=True)
class PositionAudit:
    position_id: int
    profile: str
    symbol: str
    entry_price: Decimal
    demo: DemoExit
    demo_pnl: Decimal
    """Realized on the Demo (fees included)."""
    demo_trigger: Exit | None
    """The same replay on the Demo tape (its trigger explains the Demo fill)."""
    real: Exit | None
    """Replay on the production tape (``None``: no exit until the horizon)."""

    @property
    def real_pnl(self) -> Decimal | None:
        """Demo PnL with the exit repriced at the production fill (same quantity and fees)."""
        if self.real is None:
            return None
        return self.demo_pnl + (self.real.price - self.demo.price) * self.demo.qty

    @property
    def confirmed(self) -> bool:
        """Production would have left by the same leg, at about the same moment and price."""
        return (
            self.real is not None
            and self.real.reason == self.demo.reason
            and abs(self.real.time_ms - self.demo.time_ms) <= CONFIRM_TOLERANCE_MS
            and abs(self.real.price / self.demo.price - 1) <= CONFIRM_PRICE_TOLERANCE
        )

    @property
    def demo_slip(self) -> Decimal | None:
        """Demo fill against the trigger rebuilt from the Demo tape (fraction)."""
        trigger = self.demo_trigger.trigger if self.demo_trigger else None
        return self.demo.price / trigger - 1 if trigger else None

    @property
    def real_slip(self) -> Decimal | None:
        """Production fill against its trigger (fraction)."""
        trigger = self.real.trigger if self.real else None
        return self.real.price / trigger - 1 if self.real and trigger else None


async def audit_position(
    store: Store,
    position: Position,
    *,
    demo_tape: Tape,
    real_tape: Tape,
    horizon_ms: int,
    now_ms: int,
) -> PositionAudit | None:
    """Replays one closed position on both tapes; ``None`` if it never had an entry or exit."""
    fills = await store.fills_for(position.id)
    buys = [f.trade_time for f in fills if f.is_buyer]
    if not buys or position.entry_price is None or not any(not f.is_buyer for f in fills):
        return None
    exit_ = demo_exit(await store.orders_for(position.id), fills)
    segments = segments_from(await store.intents_for(position.id), min(buys))
    market_at = exit_.time_ms if exit_.reason == MARKET else None
    demo_trigger = await replay(
        demo_tape,
        position.symbol,
        segments,
        until_ms=exit_.time_ms + 1000,
        market_exit_ms=market_at,
    )
    real = await replay(
        real_tape,
        position.symbol,
        segments,
        until_ms=min(exit_.time_ms + horizon_ms, now_ms),
        market_exit_ms=market_at,
    )
    return PositionAudit(
        position.id,
        position.profile,
        position.symbol,
        position.entry_price,
        exit_,
        position.realized_pnl or Decimal(0),
        demo_trigger,
        real,
    )


# ---------------------------------------------------------------------- report
def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M:%S")


def _slips(values: Sequence[Decimal | None]) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return "n=0"
    return f"n={len(known)}, median {_pct(statistics.median(known))}, worst {_pct(min(known))}"


def summary(audits: Sequence[PositionAudit]) -> dict[str, Any]:
    """Totals of the audit (PnL in the quote asset)."""
    repriced = [a for a in audits if a.real_pnl is not None]
    demo_total = sum((a.demo_pnl for a in repriced), Decimal(0))
    real_total = sum((a.real_pnl or Decimal(0) for a in repriced), Decimal(0))
    legs = [a for a in audits if a.demo.reason != MARKET]
    return {
        "positions": len(audits),
        "confirmed": sum(a.confirmed for a in audits),
        "diverged": [a.position_id for a in audits if not a.confirmed],
        "open_at_horizon": [a.position_id for a in audits if a.real is None],
        "demo_pnl": demo_total,
        "real_pnl": real_total,
        "difference": real_total - demo_total,
        "demo_slip": _slips([a.demo_slip for a in legs]),
        "real_slip": _slips([a.real_slip for a in audits]),
        "real_worst": _slips(
            [a.real.worst / a.real.trigger - 1 for a in audits if a.real and a.real.trigger]
        ),
    }


def render(audits: Sequence[PositionAudit]) -> str:
    """Markdown report: one row per position, then the totals."""
    lines = [
        "| # | Profile | Symbol | Demo exit | Demo fill vs trigger | Production exit "
        "| Production fill vs trigger | Demo PnL | Production PnL |",
        "|--:|---|---|---|--:|---|--:|--:|--:|",
    ]
    for a in sorted(audits, key=lambda a: a.position_id):
        if a.real is None:
            real = "open at the horizon"
        else:
            real = f"{a.real.reason} {_time(a.real.time_ms)}"
        mark = "" if a.confirmed else " **(diverged)**"
        real_pnl = "n/a" if a.real_pnl is None else f"{a.real_pnl:+.2f}"
        demo = f"{a.demo.reason} {_time(a.demo.time_ms)}"
        lines.append(
            f"| {a.position_id} | {a.profile} | {a.symbol} | {demo} | {_pct(a.demo_slip)}"
            f" | {real}{mark} | {_pct(a.real_slip)} | {a.demo_pnl:+.2f} | {real_pnl} |"
        )
    totals = summary(audits)
    lines += [
        "",
        f"- Positions: {totals['positions']}, confirmed by production: {totals['confirmed']}"
        f"; diverged: {totals['diverged'] or 'none'}"
        f"; no production exit until the horizon: {totals['open_at_horizon'] or 'none'}",
        f"- PnL of the repriced positions: Demo {totals['demo_pnl']:+.2f}"
        f", production {totals['real_pnl']:+.2f} (difference {totals['difference']:+.2f})",
        f"- Demo fill vs trigger: {totals['demo_slip']}",
        f"- Production fill vs trigger: {totals['real_slip']}"
        f"; lowest trade in {WORST_WINDOW_MS} ms: {totals['real_worst']}",
    ]
    return "\n".join(lines)
