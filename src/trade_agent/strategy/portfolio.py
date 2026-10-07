"""Seleção e dimensionamento das entradas de um perfil (função pura).

Regras (doc 03, §6.3):

* ``score_final = (1 − w)·score_TA + w·(sentimento·confiança)`` quando há leitura do LLM
  com confiança mínima; caso contrário, apenas o score técnico;
* veto do LLM exclui o ativo;
* ranking por score final, respeitando vagas, reserva de caixa, limites por *tier* e
  "uma posição por ativo";
* tamanho = ``capital × risco_por_trade / distância_do_stop``, limitado por
  ``max_position_pct``, orçamento e limite do *tier*, e escalado pelo
  ``exposure_multiplier`` (regime de mercado).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trade_agent.execution.orders import EntryMode, EntryOrder, ProtectionPolicy
from trade_agent.market.universe import Tier, UniverseMember
from trade_agent.signals import Signal
from trade_agent.strategy.profiles import ProfileConfig

ZERO = Decimal(0)
BIPS = Decimal(10_000)
PCT = Decimal(100)


@dataclass(frozen=True, slots=True)
class Holding:
    """Posição ativa (qualquer perfil) considerada nos limites."""

    profile: str
    symbol: str
    tier: Tier
    cost: Decimal


@dataclass(frozen=True, slots=True)
class MarketOpinion:
    """Leitura do analista LLM para um ativo (Fase 4)."""

    sentiment: Decimal
    confidence: Decimal
    veto: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    member: UniverseMember
    signal: Signal
    bid: Decimal
    ask: Decimal
    opinion: MarketOpinion | None = None

    def final_score(self, profile: ProfileConfig) -> Decimal:
        technical = Decimal(str(self.signal.score))
        opinion = self.opinion
        weight = profile.llm.weight
        if opinion is None or weight == 0 or opinion.confidence < profile.llm.min_confidence:
            return technical
        return (1 - weight) * technical + weight * opinion.sentiment * opinion.confidence


@dataclass(frozen=True, slots=True)
class TradeIdea:
    profile: str
    profile_code: str
    symbol: str
    tier: Tier
    setup: str
    score: Decimal
    entry: EntryOrder
    policy: ProtectionPolicy
    notional: Decimal
    risk: Decimal


@dataclass(frozen=True, slots=True)
class PlanResult:
    ideas: tuple[TradeIdea, ...]
    rejections: tuple[tuple[str, str], ...]
    """Pares ``(símbolo, motivo)``, para auditoria das decisões."""


def plan_entries(
    *,
    name: str,
    profile: ProfileConfig,
    capital: Decimal,
    holdings: Sequence[Holding],
    candidates: Sequence[Candidate],
    exposure_multiplier: Decimal = Decimal(1),
    one_position_per_asset: bool = True,
) -> PlanResult:
    own = [h for h in holdings if h.profile == name]
    held = {h.symbol for h in (holdings if one_position_per_asset else own)}
    allocation = profile.allocation
    # O exposure_multiplier do analista escala só o tamanho de cada posição (abaixo), não o
    # número de vagas: aplicado às duas, a cautela contava duas vezes, e com 2 vagas qualquer
    # leitura abaixo de 1,0 cortava a capacidade do perfil pela metade (D-030).
    slots = allocation.max_open_positions - len(own)
    budget = capital * (1 - allocation.cash_reserve_pct) - sum((h.cost for h in own), ZERO)
    tier_used = {tier: sum((h.cost for h in own if h.tier is tier), ZERO) for tier in Tier}
    risk_budget = capital * allocation.risk_per_trade_pct / PCT

    ideas: list[TradeIdea] = []
    rejections: list[tuple[str, str]] = []
    ranked = sorted(candidates, key=lambda c: c.final_score(profile), reverse=True)
    for candidate in ranked:
        symbol, tier = candidate.member.symbol, candidate.member.tier
        score = candidate.final_score(profile)
        reason: str | None = None
        if candidate.opinion is not None and candidate.opinion.veto:
            reason = "veto do analista"
        elif candidate.signal.setup is None:
            reason = "sem setup"
        elif score < Decimal(str(profile.entry.min_score)):
            reason = "score abaixo do mínimo"
        elif symbol in held:
            reason = "ativo já em carteira"
        elif exposure_multiplier <= 0:  # leitura que zera a exposição, ou on_failure: pause_entries
            reason = "exposição zero"
        elif slots <= 0:
            reason = "sem vagas no perfil"
        if reason is not None:
            rejections.append((symbol, reason))
            continue
        tier_room = capital * profile.tier_limit(tier) - tier_used[tier]
        stop_pct = profile.protection.stop_distance_pct(
            Decimal(str(candidate.signal.stop_pct)) * PCT
        )
        notional = exposure_multiplier * min(  # depois dos limites: a cautela sempre reduz
            risk_budget / (stop_pct / PCT),
            capital * allocation.max_position_pct,
            budget,
            tier_room,
        )
        if profile.entry.order is EntryMode.LIMIT_FOK:
            price = candidate.ask * (1 + Decimal(profile.entry.max_slippage_bps) / BIPS)
        else:
            price = candidate.bid
        rules = candidate.member.rules
        qty = rules.round_qty(notional / price) if notional > 0 else ZERO
        if qty <= 0 or rules.notional_violations(price, qty):
            rejections.append((symbol, "tamanho abaixo do mínimo (orçamento/tier/risco)"))
            continue
        cost = qty * price
        ideas.append(
            TradeIdea(
                profile=name,
                profile_code=profile.code,
                symbol=symbol,
                tier=tier,
                setup=candidate.signal.setup or "",
                score=score,
                entry=EntryOrder(symbol, qty, price, profile.entry.order),
                policy=profile.protection.policy(stop_pct),
                notional=cost,
                risk=cost * stop_pct / PCT,
            )
        )
        slots -= 1
        budget -= cost
        tier_used[tier] += cost
        held.add(symbol)
    return PlanResult(ideas=tuple(ideas), rejections=tuple(rejections))
