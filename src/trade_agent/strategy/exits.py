"""Regras de saída por decisão (rotação, tempo, veto) e ajuste de *break-even*.

As saídas por preço (take-profit e stop) acontecem na própria Binance (OCO); aqui ficam
apenas as saídas que dependem de análise.
"""

from datetime import timedelta
from decimal import Decimal

from trade_agent.execution.orders import FixedStop, Protection, TrailingTakeProfit
from trade_agent.execution.positions import Position
from trade_agent.signals import Signal
from trade_agent.strategy.profiles import ProfileConfig

PCT = Decimal(100)
DEFAULT_FEE_BUFFER = Decimal("0.003")
"""Margem acima do preço de entrada no *break-even* para cobrir as taxas de ida e volta."""


def next_weak_cycles(previous: int, signal: Signal | None, profile: ProfileConfig) -> int:
    """Conta ciclos consecutivos com score no nível de saída (zera ao melhorar)."""
    if signal is not None and signal.score <= profile.exits.exit_score:
        return previous + 1
    return 0


def exit_reason(
    *,
    profile: ProfileConfig,
    age: timedelta | None,
    weak_cycles: int,
    veto: bool = False,
) -> str | None:
    """Motivo para encerrar a posição por decisão, ou ``None`` para mantê-la."""
    if veto:
        return "veto do analista"
    max_holding = profile.protection.max_holding
    if max_holding is not None and age is not None and age >= max_holding:
        return "tempo máximo de permanência"
    if weak_cycles >= profile.exits.exit_after_cycles:
        return "rotação: score no nível de saída"
    return None


def break_even_protection(
    position: Position,
    profile: ProfileConfig,
    price: Decimal,
    *,
    fee_buffer: Decimal = DEFAULT_FEE_BUFFER,
) -> Protection | None:
    """Nova proteção com stop no *break-even* quando o ganho atinge ``break_even_after_r``.

    Só se aplica a stops fixos acima do nível atual; retorna ``None`` quando não há ajuste.
    """
    policy = position.policy
    trigger_r = profile.protection.break_even_after_r
    entry = position.entry_price
    if trigger_r is None or entry is None or policy.stop_pct is None:
        return None
    risk_pct = policy.stop_pct / PCT
    if price < entry * (1 + risk_pct * Decimal(str(trigger_r))):
        return None
    new_stop = entry * (1 + fee_buffer)
    if new_stop <= entry * (1 - risk_pct) or new_stop >= price:
        return None
    current = policy.resolve(entry)
    take_profit = current.take_profit
    if isinstance(take_profit, TrailingTakeProfit) and take_profit.activation_price <= price:
        take_profit = TrailingTakeProfit(price * (1 + fee_buffer), take_profit.trailing_delta_bips)
    return Protection(take_profit, FixedStop(new_stop))
