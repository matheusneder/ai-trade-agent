"""Montagem e validação das ordens do agente (OPOCO de entrada, OCO de proteção, saída).

Os construtores arredondam preços e quantidades de forma **conservadora** e validam
todas as regras do símbolo antes de qualquer envio, levantando
:class:`~trade_agent.exchange.rules.OrderValidationError` com a lista de violações.

Arredondamentos:

* entrada FOK (compra "marketable"): preço para cima; entrada maker: para baixo;
* ativação/alvo do take-profit: para cima; preço de stop: para baixo;
* quantidades: sempre para baixo.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trade_agent.exchange.models import OrderSide, OrderType, TimeInForce
from trade_agent.exchange.rules import OrderValidationError, Rounding, SymbolRules
from trade_agent.exchange.serialization import ParamValue
from trade_agent.execution.ids import OrderIdSet

BIPS = Decimal(10_000)
DEFAULT_FEE_BUFFER = Decimal("0.002")
"""Margem para a comissão descontada da quantidade recebida (validação de notional)."""


class EntryMode(StrEnum):
    LIMIT_FOK = "limit_fok"
    """Compra LIMIT FOK a preço executável: tudo ou nada, sem parcial desprotegida."""

    LIMIT_MAKER_GTC = "limit_maker_gtc"
    """Compra LIMIT_MAKER no livro (taxa menor; exige timeout e tratamento de parcial)."""


@dataclass(frozen=True, slots=True)
class TrailingTakeProfit:
    """``TAKE_PROFIT`` de venda: ativa em ``activation_price`` e segue o topo."""

    activation_price: Decimal
    trailing_delta_bips: int


@dataclass(frozen=True, slots=True)
class LimitTakeProfit:
    """``LIMIT_MAKER`` de venda no alvo fixo."""

    price: Decimal


@dataclass(frozen=True, slots=True)
class FixedStop:
    """``STOP_LOSS`` de venda (a mercado) no preço de stop."""

    stop_price: Decimal


@dataclass(frozen=True, slots=True)
class TrailingStop:
    """``STOP_LOSS`` de venda com trailing desde a colocação (sem preço fixo)."""

    trailing_delta_bips: int


type TakeProfit = TrailingTakeProfit | LimitTakeProfit
type StopLoss = FixedStop | TrailingStop


@dataclass(frozen=True, slots=True)
class Protection:
    take_profit: TakeProfit
    stop: StopLoss

    def effective_stop_price(self, reference: Decimal) -> Decimal:
        """Preço em que o stop dispararia a partir de ``reference`` (sem nova alta)."""
        if isinstance(self.stop, FixedStop):
            return self.stop.stop_price
        return reference * (1 - Decimal(self.stop.trailing_delta_bips) / BIPS)


@dataclass(frozen=True, slots=True)
class EntryOrder:
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    mode: EntryMode = EntryMode.LIMIT_FOK


type Params = dict[str, ParamValue]


def _prefixed(prefix: str, leg: Params) -> Params:
    return {f"{prefix}{key}": value for key, value in leg.items()}


def _take_profit_leg(
    take_profit: TakeProfit,
    rules: SymbolRules,
    reference: Decimal,
    client_id: str,
    problems: list[str],
) -> Params:
    if isinstance(take_profit, TrailingTakeProfit):
        activation = rules.round_price(take_profit.activation_price, Rounding.UP)
        problems += rules.price_violations(activation, "ativação do take-profit")
        problems += rules.trailing_violations(
            take_profit.trailing_delta_bips, side=OrderSide.SELL, order_type=OrderType.TAKE_PROFIT
        )
        if not rules.supports(OrderType.TAKE_PROFIT):
            problems.append("símbolo não aceita TAKE_PROFIT")
        if activation <= reference:
            problems.append("ativação do take-profit deve ficar acima do preço de referência")
        return {
            "Type": OrderType.TAKE_PROFIT,
            "StopPrice": activation,
            "TrailingDelta": take_profit.trailing_delta_bips,
            "ClientOrderId": client_id,
        }
    price = rules.round_price(take_profit.price, Rounding.UP)
    problems += rules.price_violations(price, "alvo do take-profit")
    if not rules.supports(OrderType.LIMIT_MAKER):
        problems.append("símbolo não aceita LIMIT_MAKER")
    if price <= reference:
        problems.append("alvo do take-profit deve ficar acima do preço de referência")
    return {"Type": OrderType.LIMIT_MAKER, "Price": price, "ClientOrderId": client_id}


def _stop_leg(
    stop: StopLoss,
    rules: SymbolRules,
    reference: Decimal,
    client_id: str,
    problems: list[str],
) -> Params:
    if not rules.supports(OrderType.STOP_LOSS):
        problems.append("símbolo não aceita STOP_LOSS")
    if isinstance(stop, FixedStop):
        stop_price = rules.round_price(stop.stop_price, Rounding.DOWN)
        problems += rules.price_violations(stop_price, "preço de stop")
        if stop_price >= reference:
            problems.append("stop deve ficar abaixo do preço de referência")
        return {"Type": OrderType.STOP_LOSS, "StopPrice": stop_price, "ClientOrderId": client_id}
    problems += rules.trailing_violations(
        stop.trailing_delta_bips, side=OrderSide.SELL, order_type=OrderType.STOP_LOSS
    )
    return {
        "Type": OrderType.STOP_LOSS,
        "TrailingDelta": stop.trailing_delta_bips,
        "ClientOrderId": client_id,
    }


def _exit_notional(
    protection: Protection,
    rules: SymbolRules,
    reference: Decimal,
    qty: Decimal,
    problems: list[str],
) -> None:
    stop_price = rules.round_price(protection.effective_stop_price(reference), Rounding.DOWN)
    problems += rules.notional_violations(stop_price, qty, market=True, label="perna de stop")


def _check_symbol(rules: SymbolRules, problems: list[str], *, opo: bool) -> None:
    if not rules.is_trading:
        problems.append(f"símbolo {rules.symbol} não está em negociação ({rules.status})")
    if not rules.oco_allowed:
        problems.append("símbolo não aceita OCO")
    if opo and not (rules.oto_allowed and rules.opo_allowed):
        problems.append("símbolo não aceita OPO/OTO")


def build_opoco(
    entry: EntryOrder,
    protection: Protection,
    ids: OrderIdSet,
    rules: SymbolRules,
    *,
    fee_buffer: Decimal = DEFAULT_FEE_BUFFER,
) -> Params:
    """Parâmetros de ``POST /api/v3/orderList/opoco`` (compra + OCO de venda armado)."""
    problems: list[str] = []
    _check_symbol(rules, problems, opo=True)
    fok = entry.mode is EntryMode.LIMIT_FOK
    price = rules.round_price(entry.limit_price, Rounding.UP if fok else Rounding.DOWN)
    qty = rules.round_qty(entry.quantity)
    working_type = OrderType.LIMIT if fok else OrderType.LIMIT_MAKER
    if not rules.supports(working_type):
        problems.append(f"símbolo não aceita {working_type}")
    problems += rules.price_violations(price, "preço de entrada")
    problems += rules.qty_violations(qty)
    problems += rules.notional_violations(price, qty, label="entrada")

    above = _take_profit_leg(protection.take_profit, rules, price, ids.take_profit_id, problems)
    below = _stop_leg(protection.stop, rules, price, ids.stop_id, problems)
    _exit_notional(protection, rules, price, qty * (1 - fee_buffer), problems)
    if problems:
        raise OrderValidationError(problems)

    return {
        "symbol": entry.symbol,
        "listClientOrderId": ids.list_id,
        "workingType": working_type,
        "workingSide": OrderSide.BUY,
        "workingClientOrderId": ids.entry_id,
        "workingPrice": price,
        "workingQuantity": qty,
        "workingTimeInForce": TimeInForce.FOK if fok else None,
        "pendingSide": OrderSide.SELL,
        **_prefixed("pendingAbove", above),
        **_prefixed("pendingBelow", below),
        "newOrderRespType": "FULL",
    }


def build_oco(
    symbol: str,
    quantity: Decimal,
    protection: Protection,
    ids: OrderIdSet,
    rules: SymbolRules,
    *,
    reference_price: Decimal,
) -> Params:
    """Parâmetros de ``POST /api/v3/orderList/oco`` para proteger uma posição existente."""
    problems: list[str] = []
    _check_symbol(rules, problems, opo=False)
    qty = rules.round_qty(quantity)
    problems += rules.qty_violations(qty)
    above = _take_profit_leg(
        protection.take_profit, rules, reference_price, ids.take_profit_id, problems
    )
    below = _stop_leg(protection.stop, rules, reference_price, ids.stop_id, problems)
    _exit_notional(protection, rules, reference_price, qty, problems)
    if problems:
        raise OrderValidationError(problems)
    return {
        "symbol": symbol,
        "listClientOrderId": ids.list_id,
        "side": OrderSide.SELL,
        "quantity": qty,
        **_prefixed("above", above),
        **_prefixed("below", below),
        "newOrderRespType": "FULL",
    }


def build_market_sell(
    symbol: str,
    quantity: Decimal,
    client_id: str,
    rules: SymbolRules,
    *,
    reference_price: Decimal,
) -> Params:
    """Parâmetros de ``POST /api/v3/order`` para venda a mercado (saída/fail-safe)."""
    qty = rules.round_qty(quantity, market=True)
    problems = rules.qty_violations(qty, market=True)
    problems += rules.notional_violations(reference_price, qty, market=True, label="venda")
    if not rules.supports(OrderType.MARKET):
        problems.append("símbolo não aceita MARKET")
    if problems:
        raise OrderValidationError(problems)
    return {
        "symbol": symbol,
        "side": OrderSide.SELL,
        "type": OrderType.MARKET,
        "quantity": qty,
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
    }


class TakeProfitMode(StrEnum):
    TRAILING = "trailing"
    LIMIT = "limit"


class StopMode(StrEnum):
    FIXED = "fixed"
    TRAILING = "trailing"


PCT = Decimal(100)


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    """Proteção expressa em percentuais relativos ao preço de entrada (usada pelos perfis).

    * take-profit ``trailing``: ativa em ``+take_profit_pct`` e segue o topo com
      ``take_profit_trailing_bips``; ``limit``: alvo fixo em ``+take_profit_pct``;
    * stop ``fixed``: ``-stop_pct``; ``trailing``: ``stop_trailing_bips`` desde a entrada.
    """

    take_profit_mode: TakeProfitMode
    take_profit_pct: Decimal
    stop_mode: StopMode
    take_profit_trailing_bips: int | None = None
    stop_pct: Decimal | None = None
    stop_trailing_bips: int | None = None

    def __post_init__(self) -> None:
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct deve ser positivo")
        if self.take_profit_mode is TakeProfitMode.TRAILING and not self.take_profit_trailing_bips:
            raise ValueError("take-profit trailing exige take_profit_trailing_bips")
        fixed_ok = self.stop_pct is not None and 0 < self.stop_pct < 100
        if self.stop_mode is StopMode.FIXED and not fixed_ok:
            raise ValueError("stop fixo exige stop_pct entre 0 e 100")
        if self.stop_mode is StopMode.TRAILING and not self.stop_trailing_bips:
            raise ValueError("stop trailing exige stop_trailing_bips")

    def resolve(self, reference: Decimal) -> Protection:
        """Converte a política em preços concretos a partir de ``reference``."""
        target = reference * (1 + self.take_profit_pct / PCT)
        take_profit: TakeProfit
        # Os campos opcionais usados abaixo foram validados em __post_init__.
        if self.take_profit_mode is TakeProfitMode.TRAILING:
            take_profit = TrailingTakeProfit(target, self.take_profit_trailing_bips or 0)
        else:
            take_profit = LimitTakeProfit(target)
        stop: StopLoss
        if self.stop_mode is StopMode.FIXED:
            stop = FixedStop(reference * (1 - (self.stop_pct or Decimal(0)) / PCT))
        else:
            stop = TrailingStop(self.stop_trailing_bips or 0)
        return Protection(take_profit, stop)
