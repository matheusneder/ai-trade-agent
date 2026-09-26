"""Domínio das posições: estados, transições válidas e cálculo de resultado.

Máquina de estados (doc 03, §9.2)::

    PLANNED → ENTRY_SENT → (PARTIAL) → PROTECTED ⇄ ADJUSTING
                                         ↓ ↑           ↓
                                      UNPROTECTED ⇄ EXITING → CLOSED
    EXITING → PROTECTED (a saída falhou e a proteção segue ativa na exchange)
    PLANNED/ENTRY_SENT → REJECTED (entrada não executada)
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from trade_agent.exchange.serialization import format_decimal
from trade_agent.execution.orders import EntryMode, ProtectionPolicy, StopMode, TakeProfitMode

ZERO = Decimal(0)


class PositionState(StrEnum):
    PLANNED = "planned"
    """Intenção gravada; nada enviado ainda (ou envio sem confirmação)."""

    ENTRY_SENT = "entry_sent"
    """OPOCO aceito; entrada ainda não executada (maker) ou resultado em confirmação."""

    PARTIAL = "partial"
    """Entrada GTC parcialmente executada (a parte executada ainda não tem OCO)."""

    PROTECTED = "protected"
    ADJUSTING = "adjusting"
    UNPROTECTED = "unprotected"
    EXITING = "exiting"
    CLOSED = "closed"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (PositionState.CLOSED, PositionState.REJECTED)


S = PositionState
TRANSITIONS: dict[PositionState, frozenset[PositionState]] = {
    S.PLANNED: frozenset({S.ENTRY_SENT, S.PROTECTED, S.REJECTED, S.CLOSED}),
    S.ENTRY_SENT: frozenset({S.PROTECTED, S.PARTIAL, S.REJECTED, S.CLOSED, S.UNPROTECTED}),
    S.PARTIAL: frozenset({S.PROTECTED, S.UNPROTECTED, S.EXITING, S.CLOSED}),
    S.PROTECTED: frozenset({S.ADJUSTING, S.UNPROTECTED, S.EXITING, S.CLOSED}),
    S.ADJUSTING: frozenset({S.PROTECTED, S.UNPROTECTED, S.EXITING, S.CLOSED}),
    S.UNPROTECTED: frozenset({S.PROTECTED, S.EXITING, S.CLOSED}),
    S.EXITING: frozenset({S.CLOSED, S.PROTECTED, S.UNPROTECTED}),
    S.CLOSED: frozenset(),
    S.REJECTED: frozenset(),
}

ACTIVE_STATES = frozenset(s for s in PositionState if not s.is_terminal)


class InvalidTransitionError(ValueError):
    def __init__(self, current: PositionState, target: PositionState) -> None:
        super().__init__(f"transição inválida: {current} → {target}")
        self.current = current
        self.target = target


def ensure_transition(current: PositionState, target: PositionState) -> None:
    """Valida a transição; permanecer no mesmo estado é sempre permitido."""
    if current is not target and target not in TRANSITIONS[current]:
        raise InvalidTransitionError(current, target)


class ExitReason(StrEnum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    MANUAL = "manual"
    DECISION = "decision"
    FAILSAFE = "failsafe"
    RESIDUAL = "residual"
    """Saldo remanescente abaixo do mínimo negociável (não pode ser protegido nem vendido)."""
    ENTRY_REJECTED = "entry_rejected"


# ---------------------------------------------------------------------- política (JSON)
def policy_to_json(policy: ProtectionPolicy) -> dict[str, Any]:
    return {
        "take_profit_mode": policy.take_profit_mode.value,
        "take_profit_pct": str(policy.take_profit_pct),
        "take_profit_trailing_bips": policy.take_profit_trailing_bips,
        "stop_mode": policy.stop_mode.value,
        "stop_pct": None if policy.stop_pct is None else str(policy.stop_pct),
        "stop_trailing_bips": policy.stop_trailing_bips,
    }


def policy_from_json(data: dict[str, Any]) -> ProtectionPolicy:
    return ProtectionPolicy(
        take_profit_mode=TakeProfitMode(data["take_profit_mode"]),
        take_profit_pct=Decimal(data["take_profit_pct"]),
        take_profit_trailing_bips=data.get("take_profit_trailing_bips"),
        stop_mode=StopMode(data["stop_mode"]),
        stop_pct=None if data.get("stop_pct") is None else Decimal(data["stop_pct"]),
        stop_trailing_bips=data.get("stop_trailing_bips"),
    )


# ---------------------------------------------------------------------- posição
@dataclass(frozen=True, slots=True)
class Position:
    """Instantâneo imutável de uma posição (persistido em ``positions``)."""

    id: int
    profile: str
    decision_id: str
    symbol: str
    base_asset: str
    quote_asset: str
    state: PositionState
    entry_mode: EntryMode
    policy: ProtectionPolicy
    planned_qty: Decimal
    planned_price: Decimal
    protection_list_id: str
    """Lista de proteção atual (``seq`` 0 = OPOCO de entrada; 1.. = OCOs posteriores)."""
    protection_seq: int = 0
    entry_qty: Decimal | None = None
    entry_quote: Decimal | None = None
    entry_price: Decimal | None = None
    protected_qty: Decimal | None = None
    exit_quote: Decimal | None = None
    exit_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    exit_reason: str | None = None
    fees: dict[str, str] = field(default_factory=dict)
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------- resultado
@dataclass(frozen=True, slots=True)
class FillSummary:
    """Agregado das execuções de uma ou mais ordens."""

    base_qty: Decimal
    quote_qty: Decimal
    fees: dict[str, Decimal]

    @property
    def avg_price(self) -> Decimal:
        return self.quote_qty / self.base_qty if self.base_qty else ZERO


class FillLike(Protocol):
    @property
    def qty(self) -> Decimal: ...
    @property
    def quote_qty(self) -> Decimal: ...
    @property
    def commission(self) -> Decimal: ...
    @property
    def commission_asset(self) -> str: ...


def summarize_fills(trades: Iterable[FillLike]) -> FillSummary:
    base = quote = ZERO
    fees: dict[str, Decimal] = {}
    for trade in trades:
        base += trade.qty
        quote += trade.quote_qty
        fees[trade.commission_asset] = fees.get(trade.commission_asset, ZERO) + trade.commission
    return FillSummary(base_qty=base, quote_qty=quote, fees=fees)


def net_received_base(entry: FillSummary, base_asset: str) -> Decimal:
    """Quantidade líquida recebida na compra (comissão descontada quando paga no ativo base)."""
    return entry.base_qty - entry.fees.get(base_asset, ZERO)


def realized_pnl(
    entry: FillSummary,
    exit_: FillSummary,
    *,
    base_asset: str,
    quote_asset: str,
) -> Decimal:
    """Resultado em moeda de cotação: recebido na venda − pago na compra − taxas.

    Taxas em ativo base são convertidas pelo preço médio de saída; taxas em outros ativos
    (ex.: BNB) não são convertidas aqui e ficam registradas à parte.
    """
    fees_quote = entry.fees.get(quote_asset, ZERO) + exit_.fees.get(quote_asset, ZERO)
    fees_base = exit_.fees.get(base_asset, ZERO)
    return exit_.quote_qty - entry.quote_qty - fees_quote - fees_base * exit_.avg_price


def merge_fees(*summaries: FillSummary) -> dict[str, str]:
    total: dict[str, Decimal] = {}
    for summary in summaries:
        for asset, amount in summary.fees.items():
            total[asset] = total.get(asset, ZERO) + amount
    return {asset: format_decimal(amount) for asset, amount in sorted(total.items())}
