"""Texts of the operator queries (``/status``, ``/positions``, ``/pnl``, ``/report``,
``/config``)."""

import hashlib
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trade_agent.exchange.serialization import format_decimal
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.persistence.store import Store
from trade_agent.risk.conditions import StopConditions
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.state import GLOBAL, ScopeState
from trade_agent.strategy.profiles import StrategyConfig

PERIODS = {"dia": timedelta(days=1), "semana": timedelta(days=7), "mes": timedelta(days=30)}


def _state_line(scope: str, state: ScopeState) -> str:
    detail = f" — {state.reason}" if state.reason else ""
    until = f" (até {state.until:%d/%m %H:%M} UTC)" if state.until else ""
    return f"{scope}: {state.state.value}{until}{detail}"


async def positions_text(store: Store) -> str:
    active = await store.active_positions()
    lines = [f"Posições ativas: {len(active)}"]
    for p in active:
        entry = format_decimal(p.entry_price) if p.entry_price is not None else "—"
        qty = p.protected_qty or p.entry_qty
        lines.append(
            f"• {p.symbol} [{p.profile}] {p.state.value} entrada {entry} "
            f"qtd {format_decimal(qty) if qty is not None else '—'}"
        )
    return "\n".join(lines)


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
    lines.append(await positions_text(store))
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


async def pnl_text(store: Store, strategy: StrategyConfig, args: list[str], now: datetime) -> str:
    period = args[0].lower() if args else "dia"
    if period not in PERIODS:
        return f"Período inválido. Use: {', '.join(PERIODS)}."
    since = now - PERIODS[period]
    closed = [p for p in await store.closed_positions() if p.closed_at and p.closed_at >= since]
    names = {p.code: name for name, p in strategy.profiles.items()}
    total = sum((p.realized_pnl or Decimal(0) for p in closed), Decimal(0))
    wins = sum(1 for p in closed if (p.realized_pnl or 0) > 0)
    lines = [
        f"PnL realizado ({period}): {format_decimal(total)} {strategy.account.quote_asset} "
        f"em {len(closed)} trades ({wins} com ganho)"
    ]
    by_profile: dict[str, Decimal] = {}
    for p in closed:
        name = names.get(p.profile, p.profile)
        by_profile[name] = by_profile.get(name, Decimal(0)) + (p.realized_pnl or Decimal(0))
    lines += [f"• {name}: {format_decimal(value)}" for name, value in sorted(by_profile.items())]
    return "\n".join(lines)


async def report_text(research: ResearchStore) -> str:
    report = await research.latest_report(status="ok")
    if report is None or report.view is None:
        return "Analista: sem leitura válida."
    view = report.view
    lines = [
        f"Leitura de {report.as_of:%d/%m %H:%M} UTC ({report.model}, "
        f"US$ {report.cost_usd:.4f}): regime {view['market_regime']}, "
        f"exposição {view['exposure_multiplier']}, sentimento {view['global_sentiment']}"
    ]
    lines += [f"⚠️ {flag}" for flag in view.get("global_risk_flags", [])]
    for asset in view.get("assets", []):
        veto = " VETO" if asset["veto"] else ""
        lines.append(
            f"• {asset['asset']}{veto}: {asset['sentiment']:+.2f} "
            f"(confiança {asset['confidence']:.2f}) — {asset['rationale']}"
        )
    return "\n".join(lines)


def config_hash(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def config_text(strategy: StrategyConfig, conditions: StopConditions, digest: str) -> str:
    lines = [f"Configuração {digest} — capital gerido {strategy.account.managed_capital}"]
    for name, p in strategy.enabled_profiles().items():
        take = p.protection.take_profit
        lines.append(
            f"• {name} ({p.code}): {p.timeframe}, capital {strategy.profile_capital(name)}, "
            f"risco/trade {p.allocation.risk_per_trade_pct}%, TP {take.activation_pct}%"
        )
    active = [name for name, t in conditions.global_ if t is not None]
    lines.append(f"Condições de parada ativas: {', '.join(active)}")
    return "\n".join(lines)
