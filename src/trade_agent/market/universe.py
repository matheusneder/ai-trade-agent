"""Universo de negociação: filtros de elegibilidade e *tiers* por volume (dados da Binance).

A seleção é uma função pura (:func:`select_universe`); :func:`build_universe` apenas coleta
os dados necessários na exchange.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import structlog

from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceError
from trade_agent.exchange.models import BookTicker, Ticker24h
from trade_agent.exchange.rules import SymbolRules

log = structlog.get_logger(__name__)

BIPS = Decimal(10_000)

# Ativos base que não fazem sentido operar contra USDT (stablecoins, moedas fiduciárias).
STABLE_OR_FIAT = frozenset(
    {
        "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PYUSD", "USDE", "USD1",
        "RLUSD", "XUSD", "BFUSD", "EUR", "EURI", "AEUR", "TRY", "BRL", "GBP", "JPY", "ARS",
        "MXN", "ZAR", "PLN", "RON", "UAH", "COP", "CZK", "IDRT", "BIDR", "PAXG", "XAUT",
    }
)  # fmt: skip
_LEVERAGED = re.compile(r"^.+(UP|DOWN|BULL|BEAR)$")


class Tier(StrEnum):
    CORE = "core"
    LARGE = "large"
    MID = "mid"
    SMALL = "small"


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    quote_asset: str = "USDT"
    core_assets: tuple[str, ...] = ("BTC", "ETH")
    min_quote_volume_24h: Decimal = Decimal(5_000_000)
    max_spread_bps: Decimal = Decimal(20)
    min_history_days: int = 30
    large_rank: int = 20
    """Posições 1..N por volume (excluindo o *core*) formam o tier *large*."""
    mid_rank: int = 60
    excluded_assets: frozenset[str] = STABLE_OR_FIAT


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    base_asset: str
    tier: Tier
    rank: int
    quote_volume_24h: Decimal
    spread_bps: Decimal
    rules: SymbolRules


@dataclass(frozen=True, slots=True)
class Universe:
    members: tuple[UniverseMember, ...]
    excluded: Mapping[str, str] = field(default_factory=dict)
    """Símbolos da moeda de cotação excluídos e o motivo."""

    def by_symbol(self) -> dict[str, UniverseMember]:
        return {m.symbol: m for m in self.members}

    def symbols(self, tiers: frozenset[Tier] | None = None) -> list[str]:
        return [m.symbol for m in self.members if tiers is None or m.tier in tiers]


def _spread_bps(book: BookTicker | None) -> Decimal | None:
    if book is None or book.bid_price <= 0 or book.ask_price <= 0:
        return None
    mid = (book.bid_price + book.ask_price) / 2
    return (book.ask_price - book.bid_price) / mid * BIPS


def _static_exclusion(rules: SymbolRules, config: UniverseConfig, delisted: set[str]) -> str | None:
    if not rules.is_trading:
        return f"status {rules.status}"
    if not (rules.oco_allowed and rules.oto_allowed and rules.opo_allowed):
        return "sem OCO/OTO/OPO"
    if not rules.allow_trailing_stop:
        return "sem trailing stop"
    if rules.base_asset in config.excluded_assets:
        return "stablecoin/fiat"
    if _LEVERAGED.match(rules.base_asset):
        return "token alavancado"
    if rules.symbol in delisted:
        return "delistagem agendada"
    return None


def prefilter(
    rules_by_symbol: Mapping[str, SymbolRules],
    tickers: Mapping[str, Ticker24h],
    books: Mapping[str, BookTicker],
    delisted: set[str],
    config: UniverseConfig,
) -> tuple[list[str], dict[str, str]]:
    """Primeira etapa (sem histórico): regras, liquidez e spread; ordena por volume."""
    passed: list[str] = []
    excluded: dict[str, str] = {}
    for symbol, rules in rules_by_symbol.items():
        if rules.quote_asset != config.quote_asset:
            continue
        reason = _static_exclusion(rules, config, delisted)
        ticker = tickers.get(symbol)
        spread = _spread_bps(books.get(symbol))
        if reason is None and (ticker is None or ticker.quote_volume < config.min_quote_volume_24h):
            reason = "volume insuficiente"
        if reason is None and (spread is None or spread > config.max_spread_bps):
            reason = "spread alto"
        if reason is None:
            passed.append(symbol)
        else:
            excluded[symbol] = reason
    passed.sort(key=lambda s: tickers[s].quote_volume, reverse=True)
    return passed, excluded


def select_universe(
    rules_by_symbol: Mapping[str, SymbolRules],
    tickers: Mapping[str, Ticker24h],
    books: Mapping[str, BookTicker],
    *,
    delisted: set[str],
    history_days: Mapping[str, int],
    config: UniverseConfig,
) -> Universe:
    """Seleção completa: pré-filtro, histórico mínimo e atribuição de *tiers*."""
    ranked, excluded = prefilter(rules_by_symbol, tickers, books, delisted, config)
    members: list[UniverseMember] = []
    rank = 0
    for symbol in ranked:
        if history_days.get(symbol, 0) < config.min_history_days:
            excluded[symbol] = "histórico curto"
            continue
        rules = rules_by_symbol[symbol]
        if rules.base_asset in config.core_assets:
            tier = Tier.CORE
        else:
            rank += 1
            tier = (
                Tier.LARGE
                if rank <= config.large_rank
                else Tier.MID
                if rank <= config.mid_rank
                else Tier.SMALL
            )
        members.append(
            UniverseMember(
                symbol=symbol,
                base_asset=rules.base_asset,
                tier=tier,
                rank=rank,
                quote_volume_24h=tickers[symbol].quote_volume,
                spread_bps=_spread_bps(books[symbol]) or Decimal(0),
                rules=rules,
            )
        )
    return Universe(members=tuple(members), excluded=excluded)


async def build_universe(api: BinanceSpotApi, config: UniverseConfig) -> Universe:
    """Coleta regras, tickers, livro, delistagens e histórico, e seleciona o universo."""
    rules = await api.exchange_info()
    tickers = {t.symbol: t for t in await api.tickers_24h()}
    books = {b.symbol: b for b in await api.book_tickers()}
    try:
        delisted = await api.delist_schedule()
    except BinanceError as exc:
        log.warning("universe.delist_schedule_unavailable", error=str(exc))
        delisted = set()
    ranked, _ = prefilter(rules, tickers, books, delisted, config)
    history: dict[str, int] = {}
    for symbol in ranked:
        daily = await api.klines(symbol, "1d", limit=config.min_history_days)
        history[symbol] = len(daily)
    return select_universe(
        rules, tickers, books, delisted=delisted, history_days=history, config=config
    )
