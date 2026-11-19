"""When the followed lead traders' trades are a signal (pure functions, D-052).

A trader **buys** an asset when its net purchase of it in the window (what it bought minus
what it sold, in the quote asset) reaches ``min_buy_pct`` of its own balance, and **sells**
it on the same net sale. Only its ``max_symbols_per_lead`` largest net purchases count, so a
trader that buys everything does not weigh on every asset. An asset has a **buy consensus**
when its buyers outnumber its sellers by ``min_backers``, and a **sell consensus** on the
opposite margin.

The agent never copies a trade: a buy consensus only lets the pair's own candles decide
(``lead_consensus`` in :mod:`trade_agent.signals.local`), and the size, the stop and the
target are the profile's. :class:`LeadView` is what a decision cycle reads, and
:meth:`LeadView.apply` turns the candle-side signal into the cycle's one.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from trade_agent.leads.source import LeadTrade
from trade_agent.signals import Signal
from trade_agent.strategy.profiles import LeadConsensusConfig

PCT = Decimal(100)
OK = "ok"
NO_TRACKER = "copy trading sem rastreador"
NO_LEADS = "nenhum trader selecionado"
STALE_RANKING = "ranking de traders vencido"
NO_DATA = "copy trading sem dados recentes"
SELL_CONSENSUS = "saída: consenso de venda dos traders"
BUY, SELL = "buy", "sell"


@dataclass(frozen=True, slots=True)
class Backing:
    """The followed traders on each side of an asset within the window."""

    symbol: str
    buyers: tuple[str, ...] = ()
    sellers: tuple[str, ...] = ()
    avg_buy_price: Decimal | None = None
    """Volume-weighted price the buyers paid in the window; ``None`` without a buyer."""

    def consensus(self, min_backers: int) -> str | None:
        """``buy`` or ``sell`` when that side outnumbers the other by ``min_backers``."""
        margin = len(self.buyers) - len(self.sellers)
        if abs(margin) < min_backers:
            return None
        return BUY if margin > 0 else SELL


@dataclass(frozen=True, slots=True)
class LeadReading:
    """A followed trader's own balance and its most recent trades."""

    balance: Decimal
    trades: Sequence[LeadTrade]


@dataclass(slots=True)
class _Flow:
    net: Decimal = Decimal(0)
    bought: Decimal = Decimal(0)
    qty: Decimal = Decimal(0)


def _flows(trades: Sequence[LeadTrade], since: datetime, quote_asset: str) -> dict[str, _Flow]:
    """Per symbol, what the trader moved since ``since`` in pairs quoted in ``quote_asset``."""
    flows: defaultdict[str, _Flow] = defaultdict(_Flow)
    for trade in trades:
        if trade.at < since or trade.quote_asset != quote_asset:
            continue
        flow = flows[trade.symbol]
        if trade.buy:
            flow.net += trade.quote
            flow.bought += trade.quote
            flow.qty += trade.qty
        else:
            flow.net -= trade.quote
    return flows


def backings(
    readings: Mapping[str, LeadReading],
    *,
    now: datetime,
    config: LeadConsensusConfig,
    quote_asset: str,
) -> dict[str, Backing]:
    """Each asset's buyers and sellers among ``readings`` (lead id → its reading)."""
    buyers: defaultdict[str, list[str]] = defaultdict(list)
    sellers: defaultdict[str, list[str]] = defaultdict(list)
    paid: defaultdict[str, _Flow] = defaultdict(_Flow)
    for lead in sorted(readings):
        reading = readings[lead]
        threshold = reading.balance * config.min_buy_pct / PCT
        if threshold <= 0:
            continue  # no balance to measure a purchase against
        flows = _flows(reading.trades, now - config.window, quote_asset)
        bought = sorted(
            (s for s, f in flows.items() if f.net >= threshold),
            key=lambda s: (-flows[s].net, s),
        )
        for symbol in bought[: config.max_symbols_per_lead]:
            buyers[symbol].append(lead)
            paid[symbol].bought += flows[symbol].bought
            paid[symbol].qty += flows[symbol].qty
        for symbol, flow in flows.items():
            if flow.net <= -threshold:
                sellers[symbol].append(lead)
    return {
        symbol: Backing(
            symbol=symbol,
            buyers=tuple(buyers.get(symbol, ())),
            sellers=tuple(sellers.get(symbol, ())),
            avg_buy_price=paid[symbol].bought / paid[symbol].qty if paid[symbol].qty else None,
        )
        for symbol in sorted(buyers.keys() | sellers.keys())
    }


@dataclass(frozen=True, slots=True)
class LeadView:
    """What a decision cycle reads about the followed traders."""

    status: str = OK
    """``ok``, or why there is no reading: no entry goes out then."""
    selected: int = 0
    fresh: int = 0
    """Followed traders whose trades were read recently enough to count."""
    as_of: datetime | None = None
    """The oldest of the readings that count."""
    backings: Mapping[str, Backing] = field(default_factory=dict)

    def sold(self, symbol: str, config: LeadConsensusConfig) -> bool:
        return self._consensus(symbol, config) == SELL

    def _consensus(self, symbol: str, config: LeadConsensusConfig) -> str | None:
        backing = self.backings.get(symbol)
        if self.status != OK or backing is None:
            return None
        return backing.consensus(config.min_backers)

    def apply(self, symbol: str, signal: Signal, config: LeadConsensusConfig) -> Signal:
        """The cycle's signal for ``symbol`` from its candle side.

        Without a buy consensus there is no setup, whatever the candles say. With one, the
        candles decide: a confirmed setup is ranked by the share of the followed traders
        behind it and capped ``max_premium_pct`` above what they paid, and a refused one
        keeps its reason (``filtered``), which is what the agent declined to follow. A sell
        consensus is a technical exit for a position the profile holds.
        """
        consensus = self._consensus(symbol, config)
        signal = replace(signal, exit_signal=signal.exit_signal or consensus == SELL)
        if consensus != BUY:
            return replace(signal, setup=None, filtered=None)
        backing = self.backings[symbol]
        if signal.setup is None or backing.avg_buy_price is None:
            return signal
        cap = backing.avg_buy_price * (1 + config.max_premium_pct / PCT)
        return replace(
            signal, score=len(backing.buyers) / max(self.selected, 1), max_entry_price=float(cap)
        )

    def exit_reason(
        self, symbol: str, technical: str | None, config: LeadConsensusConfig
    ) -> str | None:
        """The technical exit's reason: the sell consensus, when that is what fired it."""
        return SELL_CONSENSUS if self.sold(symbol, config) else technical

    def summary(self, config: LeadConsensusConfig) -> dict[str, object]:
        """What the cycle's event records: who was behind each asset (lead portfolio ids)."""
        return {
            "status": self.status,
            "selected": self.selected,
            "fresh": self.fresh,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "assets": {
                symbol: {
                    "buyers": list(b.buyers),
                    "sellers": list(b.sellers),
                    "avg_buy_price": None if b.avg_buy_price is None else f"{b.avg_buy_price:f}",
                    "consensus": b.consensus(config.min_backers),
                }
                for symbol, b in self.backings.items()
            },
        }
