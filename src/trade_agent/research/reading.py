"""Tradução do ``MarketView`` para as entradas da carteira (``strategy.portfolio``).

Degradação segura (doc 03, §7.3): sem leitura válida (falha, orçamento esgotado ou
leitura vencida), cada perfil segue a sua regra ``llm.on_failure``:

* ``ta_only``: TA pura com exposição normal;
* ``ta_only_reduced``: TA pura com exposição reduzida à metade;
* ``pause_entries``: nenhuma entrada nova.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trade_agent.research.models import MarketView
from trade_agent.strategy.portfolio import MarketOpinion
from trade_agent.strategy.profiles import LlmConfig

FAILURE_EXPOSURE: Mapping[str, Decimal] = {
    "ta_only": Decimal(1),
    "ta_only_reduced": Decimal("0.5"),
    "pause_entries": Decimal(0),
}


@dataclass(frozen=True, slots=True)
class MarketReading:
    exposure_multiplier: Decimal
    opinions: Mapping[str, MarketOpinion] = field(default_factory=dict)
    degraded: bool = False
    reason: str | None = None


def market_reading(
    view: MarketView | None, llm: LlmConfig, *, now: datetime, max_age: timedelta
) -> MarketReading:
    """Leitura aplicável a um perfil. Vetos e a redução de exposição valem mesmo com
    ``llm.weight = 0``: a autoridade do LLM é assimétrica e só reduz risco."""
    if view is None:
        return MarketReading(FAILURE_EXPOSURE[llm.on_failure], degraded=True, reason="sem leitura")
    if now - view.as_of > max_age:
        return MarketReading(
            FAILURE_EXPOSURE[llm.on_failure], degraded=True, reason="leitura vencida"
        )
    opinions = {
        a.asset: MarketOpinion(
            sentiment=Decimal(str(a.sentiment)),
            confidence=Decimal(str(a.confidence)),
            veto=a.veto,
        )
        for a in view.assets
    }
    return MarketReading(Decimal(str(view.exposure_multiplier)), opinions)
