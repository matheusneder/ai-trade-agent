"""Avaliação pura do estado de uma lista de proteção a partir das ordens na exchange.

Não faz I/O: recebe o instantâneo da lista (entrada, take-profit e stop) e devolve um
veredito que o serviço de posições aplica.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trade_agent.exchange.models import ListOrderStatus, Order, OrderStatus
from trade_agent.execution.ids import Leg, parse_client_id

_ACTIVE = frozenset({OrderStatus.NEW})
_ARMING = frozenset({OrderStatus.PENDING_NEW})


@dataclass(frozen=True, slots=True)
class ListSnapshot:
    list_client_order_id: str
    list_order_status: ListOrderStatus
    orders: tuple[Order, ...]

    def leg(self, leg: Leg) -> Order | None:
        for order in self.orders:
            parts = parse_client_id(order.client_order_id)
            if parts is not None and parts.leg is leg:
                return order
        return None


class VerdictKind(StrEnum):
    MISSING = "missing"
    """A lista não existe na exchange."""

    AWAITING_ENTRY = "awaiting_entry"
    """Entrada maker ainda no livro; nada executado."""

    ARMING = "arming"
    """Entrada executada; a Binance está armando o OCO (``PENDING_NEW``)."""

    PROTECTED = "protected"
    PARTIAL = "partial"
    """Entrada GTC parcialmente executada: a parte executada ainda não tem proteção."""

    EXITING = "exiting"
    """Uma perna de saída está parcialmente executada."""

    CLOSED = "closed"
    REJECTED = "rejected"
    """A entrada terminou sem nenhuma execução."""

    UNPROTECTED = "unprotected"
    """Há saldo da posição sem proteção ativa (pernas expiradas/canceladas sem execução)."""


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    reason: str
    entry: Order | None = None
    exit_leg: Order | None = None
    protected_qty: Decimal | None = None
    held_qty: Decimal | None = None


def assess(snapshot: ListSnapshot | None) -> Verdict:
    """Deriva o estado da posição a partir da sua lista de proteção atual."""
    if snapshot is None:
        return Verdict(VerdictKind.MISSING, "lista não encontrada na exchange")
    entry = snapshot.leg(Leg.ENTRY)
    take_profit = snapshot.leg(Leg.TAKE_PROFIT)
    stop = snapshot.leg(Leg.STOP)
    exits = [leg for leg in (take_profit, stop) if leg is not None]

    for leg in exits:
        if leg.status is OrderStatus.FILLED:
            reason = "take-profit executado" if leg is take_profit else "stop executado"
            return Verdict(VerdictKind.CLOSED, reason, entry=entry, exit_leg=leg)
    for leg in exits:
        if leg.status is OrderStatus.PARTIALLY_FILLED:
            return Verdict(VerdictKind.EXITING, "saída parcialmente executada", entry, leg)

    if entry is not None:
        if entry.status is OrderStatus.NEW:
            return Verdict(VerdictKind.AWAITING_ENTRY, "entrada no livro", entry=entry)
        if entry.status is OrderStatus.PARTIALLY_FILLED:
            return Verdict(
                VerdictKind.PARTIAL,
                "entrada parcialmente executada",
                entry=entry,
                held_qty=entry.executed_qty,
            )
        if entry.status is not OrderStatus.FILLED:
            if entry.executed_qty > 0:
                return Verdict(
                    VerdictKind.UNPROTECTED,
                    f"entrada {entry.status} com execução parcial sem proteção",
                    entry=entry,
                    held_qty=entry.executed_qty,
                )
            return Verdict(VerdictKind.REJECTED, f"entrada {entry.status} sem execução", entry)

    statuses = {leg.status for leg in exits}
    if len(exits) == 2 and statuses <= _ACTIVE:
        return Verdict(
            VerdictKind.PROTECTED,
            "OCO ativo",
            entry=entry,
            protected_qty=exits[0].orig_qty,
        )
    if exits and statuses & _ARMING and not statuses - _ARMING - _ACTIVE:
        return Verdict(VerdictKind.ARMING, "OCO sendo armado", entry=entry)
    held = exits[0].orig_qty if exits else (entry.executed_qty if entry else None)
    return Verdict(
        VerdictKind.UNPROTECTED,
        "pernas de proteção inativas sem execução: "
        + ", ".join(sorted(f"{leg.status}/{leg.expiry_reason or '-'}" for leg in exits)),
        entry=entry,
        held_qty=held,
    )
