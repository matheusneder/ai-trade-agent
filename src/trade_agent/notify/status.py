"""Texto do ``/status``: estados operacionais, posições ativas e última leitura do analista."""

from trade_agent.exchange.serialization import format_decimal
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.persistence.store import Store
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.state import GLOBAL, ScopeState
from trade_agent.strategy.profiles import StrategyConfig


def _state_line(scope: str, state: ScopeState) -> str:
    detail = f" — {state.reason}" if state.reason else ""
    until = f" (até {state.until:%d/%m %H:%M} UTC)" if state.until else ""
    return f"{scope}: {state.state.value}{until}{detail}"


async def status_text(
    *,
    guard: RiskGuard,
    store: Store,
    research: ResearchStore,
    strategy: StrategyConfig,
    trading_enabled: bool,
) -> str:
    lines = [f"Ordens: {'habilitadas' if trading_enabled else 'SIMULAÇÃO (trava desligada)'}"]
    lines.append(_state_line(GLOBAL, await guard.state(GLOBAL)))
    for name, profile in strategy.profiles.items():
        suffix = "" if profile.enabled else " [desabilitado]"
        lines.append(_state_line(name, await guard.state(name)) + suffix)
    active = await store.active_positions()
    lines.append(f"Posições ativas: {len(active)}")
    for p in active:
        entry = format_decimal(p.entry_price) if p.entry_price is not None else "—"
        qty = p.protected_qty or p.entry_qty
        lines.append(
            f"• {p.symbol} [{p.profile}] {p.state.value} entrada {entry} "
            f"qtd {format_decimal(qty) if qty is not None else '—'}"
        )
    report = await research.latest_report(status="ok")
    if report is not None and report.view is not None:
        view = report.view
        lines.append(
            f"Analista ({report.as_of:%d/%m %H:%M} UTC): regime {view['market_regime']}, "
            f"exposição {view['exposure_multiplier']}"
        )
    else:
        lines.append("Analista: sem leitura válida")
    return "\n".join(lines)
