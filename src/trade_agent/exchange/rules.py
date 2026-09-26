"""Regras (filtros) de negociação de um símbolo e arredondamento de preço e quantidade.

Implementa as regras de ``filters.md`` da Binance que afetam as ordens do agente:
``PRICE_FILTER``, ``LOT_SIZE``, ``MARKET_LOT_SIZE``, ``NOTIONAL``/``MIN_NOTIONAL``,
``PERCENT_PRICE_BY_SIDE``, ``TRAILING_DELTA`` e limites de quantidade de ordens.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from trade_agent.exchange.models import OrderSide, OrderType
from trade_agent.exchange.serialization import format_decimal

ZERO = Decimal(0)


class Rounding(StrEnum):
    DOWN = ROUND_FLOOR
    UP = ROUND_CEILING
    NEAREST = ROUND_HALF_UP


class OrderValidationError(ValueError):
    """A ordem viola uma ou mais regras do símbolo."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


@dataclass(frozen=True, slots=True)
class TrailingDeltaBounds:
    min_above: int
    max_above: int
    min_below: int
    max_below: int


@dataclass(frozen=True, slots=True)
class PercentPriceBySide:
    bid_up: Decimal
    bid_down: Decimal
    ask_up: Decimal
    ask_down: Decimal


# Tipos cujo trailingDelta usa os limites "above" (FAQ de trailing stop da Binance).
_ABOVE_TRAILING = frozenset(
    {
        (OrderType.STOP_LOSS, OrderSide.BUY),
        (OrderType.STOP_LOSS_LIMIT, OrderSide.BUY),
        (OrderType.TAKE_PROFIT, OrderSide.SELL),
        (OrderType.TAKE_PROFIT_LIMIT, OrderSide.SELL),
    }
)


def _dec(value: Any, default: Decimal = ZERO) -> Decimal:
    return Decimal(str(value)) if value is not None else default


def _snap(value: Decimal, base: Decimal, step: Decimal, rounding: Rounding) -> Decimal:
    """Ajusta ``value`` à grade ``base + k*step`` (k inteiro)."""
    units = ((value - base) / step).to_integral_value(rounding=rounding.value)
    return base + units * step


def _on_grid(value: Decimal, base: Decimal, step: Decimal) -> bool:
    return (value - base) % step == 0


@dataclass(frozen=True, slots=True)
class SymbolRules:
    """Regras de um símbolo extraídas de ``exchangeInfo``."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    order_types: frozenset[str]
    oco_allowed: bool
    oto_allowed: bool
    opo_allowed: bool
    allow_trailing_stop: bool
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    apply_min_to_market: bool
    apply_max_to_market: bool
    percent_price_by_side: PercentPriceBySide | None = None
    trailing: TrailingDeltaBounds | None = None
    max_num_orders: int | None = None
    max_num_algo_orders: int | None = None
    max_num_order_lists: int | None = None

    # ------------------------------------------------------------------ construção
    @classmethod
    def from_exchange_info(cls, data: Mapping[str, Any]) -> "SymbolRules":
        filters: dict[str, Mapping[str, Any]] = {f["filterType"]: f for f in data["filters"]}
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        notional = filters.get("NOTIONAL")
        legacy_notional = filters.get("MIN_NOTIONAL", {})
        market_step = _dec(market_lot.get("stepSize"))
        pps = filters.get("PERCENT_PRICE_BY_SIDE")
        trailing = filters.get("TRAILING_DELTA")

        if notional is not None:
            min_notional = _dec(notional.get("minNotional"))
            max_notional = _dec(notional.get("maxNotional"))
            apply_min = bool(notional.get("applyMinToMarket", True))
            apply_max = bool(notional.get("applyMaxToMarket", False))
        else:
            min_notional = _dec(legacy_notional.get("minNotional"))
            max_notional = ZERO
            apply_min = bool(legacy_notional.get("applyToMarket", True))
            apply_max = False

        return cls(
            symbol=data["symbol"],
            status=data["status"],
            base_asset=data["baseAsset"],
            quote_asset=data["quoteAsset"],
            order_types=frozenset(data.get("orderTypes", ())),
            oco_allowed=bool(data.get("ocoAllowed", False)),
            oto_allowed=bool(data.get("otoAllowed", False)),
            opo_allowed=bool(data.get("opoAllowed", False)),
            allow_trailing_stop=bool(data.get("allowTrailingStop", False)),
            tick_size=_dec(price.get("tickSize")),
            min_price=_dec(price.get("minPrice")),
            max_price=_dec(price.get("maxPrice")),
            step_size=_dec(lot.get("stepSize")),
            min_qty=_dec(lot.get("minQty")),
            max_qty=_dec(lot.get("maxQty")),
            market_step_size=market_step,
            market_min_qty=_dec(market_lot.get("minQty")),
            market_max_qty=_dec(market_lot.get("maxQty")),
            min_notional=min_notional,
            max_notional=max_notional,
            apply_min_to_market=apply_min,
            apply_max_to_market=apply_max,
            percent_price_by_side=PercentPriceBySide(
                bid_up=_dec(pps["bidMultiplierUp"]),
                bid_down=_dec(pps["bidMultiplierDown"]),
                ask_up=_dec(pps["askMultiplierUp"]),
                ask_down=_dec(pps["askMultiplierDown"]),
            )
            if pps
            else None,
            trailing=TrailingDeltaBounds(
                min_above=int(trailing["minTrailingAboveDelta"]),
                max_above=int(trailing["maxTrailingAboveDelta"]),
                min_below=int(trailing["minTrailingBelowDelta"]),
                max_below=int(trailing["maxTrailingBelowDelta"]),
            )
            if trailing
            else None,
            max_num_orders=_opt_int(filters, "MAX_NUM_ORDERS", "maxNumOrders"),
            max_num_algo_orders=_opt_int(filters, "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders"),
            max_num_order_lists=_opt_int(filters, "MAX_NUM_ORDER_LISTS", "maxNumOrderLists"),
        )

    # ------------------------------------------------------------------ consultas
    @property
    def is_trading(self) -> bool:
        return self.status == "TRADING"

    def supports(self, order_type: OrderType) -> bool:
        return order_type.value in self.order_types

    # ------------------------------------------------------------------ arredondamento
    def round_price(self, price: Decimal, rounding: Rounding = Rounding.NEAREST) -> Decimal:
        """Ajusta o preço ao ``tickSize`` (a partir de ``minPrice``)."""
        if self.tick_size <= 0:
            return price
        return _snap(price, self.min_price, self.tick_size, rounding)

    def round_qty(self, qty: Decimal, *, market: bool = False) -> Decimal:
        """Ajusta a quantidade para baixo ao ``stepSize`` (nunca arredonda para cima).

        Ordens a mercado também respeitam o ``MARKET_LOT_SIZE`` quando ele define um passo.
        """
        grids = [(self.min_qty, self.step_size)]
        if market:
            grids.append((self.market_min_qty, self.market_step_size))
        for base, step in grids:
            if step <= 0:
                continue
            if qty < base:
                return ZERO
            qty = _snap(qty, base, step, Rounding.DOWN)
        return qty

    # ------------------------------------------------------------------ validações
    def price_violations(self, price: Decimal, label: str = "preço") -> list[str]:
        problems: list[str] = []
        if price <= 0:
            return [f"PRICE_FILTER: {label} {format_decimal(price)} deve ser positivo"]
        if self.min_price > 0 and price < self.min_price:
            problems.append(f"PRICE_FILTER: {label} {format_decimal(price)} < minPrice")
        if self.max_price > 0 and price > self.max_price:
            problems.append(f"PRICE_FILTER: {label} {format_decimal(price)} > maxPrice")
        if self.tick_size > 0 and not _on_grid(price, self.min_price, self.tick_size):
            problems.append(
                f"PRICE_FILTER: {label} {format_decimal(price)} fora do tickSize "
                f"{format_decimal(self.tick_size)}"
            )
        return problems

    def qty_violations(self, qty: Decimal, *, market: bool = False) -> list[str]:
        if qty <= 0:
            return [f"LOT_SIZE: quantidade {format_decimal(qty)} deve ser positiva"]
        problems = _lot_violations("LOT_SIZE", qty, self.min_qty, self.max_qty, self.step_size)
        if market:
            problems += _lot_violations(
                "MARKET_LOT_SIZE",
                qty,
                self.market_min_qty,
                self.market_max_qty,
                self.market_step_size,
            )
        return problems

    def notional_violations(
        self, price: Decimal, qty: Decimal, *, market: bool = False, label: str = "ordem"
    ) -> list[str]:
        notional = price * qty
        problems: list[str] = []
        if (not market or self.apply_min_to_market) and notional < self.min_notional:
            problems.append(
                f"NOTIONAL: {label} {format_decimal(notional)} < minNotional "
                f"{format_decimal(self.min_notional)}"
            )
        if (
            self.max_notional > 0
            and (not market or self.apply_max_to_market)
            and notional > self.max_notional
        ):
            problems.append(f"NOTIONAL: {label} {format_decimal(notional)} > maxNotional")
        return problems

    def trailing_violations(
        self, delta: int, *, side: OrderSide, order_type: OrderType
    ) -> list[str]:
        if not self.allow_trailing_stop:
            return ["TRAILING_DELTA: trailing stop não permitido no símbolo"]
        if self.trailing is None:
            return []
        if (order_type, side) in _ABOVE_TRAILING:
            low, high = self.trailing.min_above, self.trailing.max_above
        else:
            low, high = self.trailing.min_below, self.trailing.max_below
        if not low <= delta <= high:
            return [f"TRAILING_DELTA: {delta} fora do intervalo [{low}, {high}]"]
        return []

    def percent_price_violations(
        self, price: Decimal, *, side: OrderSide, avg_price: Decimal
    ) -> list[str]:
        pps = self.percent_price_by_side
        if pps is None:
            return []
        up, down = (
            (pps.bid_up, pps.bid_down) if side is OrderSide.BUY else (pps.ask_up, pps.ask_down)
        )
        if not avg_price * down <= price <= avg_price * up:
            return [
                f"PERCENT_PRICE_BY_SIDE: preço {format_decimal(price)} fora da faixa "
                f"[{format_decimal(avg_price * down)}, {format_decimal(avg_price * up)}]"
            ]
        return []


def _lot_violations(
    name: str, qty: Decimal, min_qty: Decimal, max_qty: Decimal, step: Decimal
) -> list[str]:
    problems: list[str] = []
    if qty < min_qty:
        problems.append(f"{name}: quantidade {format_decimal(qty)} < minQty")
    if max_qty > 0 and qty > max_qty:
        problems.append(f"{name}: quantidade {format_decimal(qty)} > maxQty")
    if step > 0 and not _on_grid(qty, min_qty, step):
        problems.append(
            f"{name}: quantidade {format_decimal(qty)} fora do stepSize {format_decimal(step)}"
        )
    return problems


def _opt_int(filters: Mapping[str, Mapping[str, Any]], name: str, key: str) -> int | None:
    item = filters.get(name)
    return int(item[key]) if item is not None else None
