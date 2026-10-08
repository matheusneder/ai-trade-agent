"""Selection and sizing of a profile's entries (pure function).

Rules (doc 03, §6.3):

* ``final_score = (1 − w)·TA_score + w·(sentiment·confidence)`` when there is an LLM
  reading with the minimum confidence; otherwise, only the technical score;
* an LLM veto excludes the asset;
* ranking by final score, honoring slots, cash reserve, *tier* limits and "one position
  per asset";
* size = ``capital × risk_per_trade / stop_distance``, capped by ``max_position_pct``,
  the budget and the *tier* limit, and scaled by the ``exposure_multiplier`` (market
  regime).
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
    """Active position (any profile) taken into account in the limits."""

    profile: str
    symbol: str
    tier: Tier
    cost: Decimal


@dataclass(frozen=True, slots=True)
class MarketOpinion:
    """The LLM analyst's reading of an asset (Phase 4)."""

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
    """``(symbol, reason)`` pairs, for auditing the decisions."""


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
    # The analyst's exposure_multiplier scales only the size of each position (below), not the
    # number of slots: applied to both, caution counted twice, and with 2 slots any reading
    # below 1.0 cut the profile's capacity in half (D-030).
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
        elif exposure_multiplier <= 0:  # reading with zero exposure, or on_failure: pause_entries
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
        notional = exposure_multiplier * min(  # after the limits: caution always reduces
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
