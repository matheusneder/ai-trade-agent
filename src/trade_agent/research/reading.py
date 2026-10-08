"""Translation of the ``MarketView`` into the portfolio's inputs (``strategy.portfolio``).

Safe degradation (doc 03, §7.3): without a valid reading (failure, budget exhausted or
expired reading), each profile follows its ``llm.on_failure`` rule:

* ``ta_only``: pure TA with normal exposure;
* ``ta_only_reduced``: pure TA with exposure cut in half;
* ``pause_entries``: no new entries.
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
    """Reading that applies to a profile. Vetoes and the exposure cut hold even with
    ``llm.weight = 0``: the LLM's authority is asymmetric and only reduces risk."""
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
