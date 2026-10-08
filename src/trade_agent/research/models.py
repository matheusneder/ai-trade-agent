"""Analyst contracts: news items, market metrics and ``MarketView`` (doc 03, §7.2).

There are two levels of model for the LLM output:

* ``*Draft``: the format requested from the model (*structured outputs*), **without**
  numeric constraints. Out-of-range values do not invalidate the whole answer; the code
  truncates them (``research.safety``), as the safety rule requires;
* ``MarketView``/``AssetView``: the already sanitized reading, with guaranteed ranges.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MarketRegime(StrEnum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"


class Horizon(StrEnum):
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"


class NewsCategory(StrEnum):
    HACK = "hack"
    DELISTING = "delisting"
    LISTING = "listing"
    REGULATORY = "regulatory"
    MACRO = "macro"
    PROJECT = "project"
    MARKET = "market"
    OTHER = "other"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================================ inputs
@dataclass(frozen=True, slots=True)
class NewsItem:
    """Collected news item or announcement (external content: **untrusted data**)."""

    source: str
    title: str
    published_at: datetime
    url: str | None = None
    summary: str = ""
    assets: tuple[str, ...] = ()

    @property
    def dedupe_key(self) -> str:
        """Deduplication key: source + URL (or the normalized title, without a URL)."""
        basis = self.url or " ".join(self.title.lower().split())
        return hashlib.sha256(f"{self.source}|{basis}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class FearGreed:
    value: int
    classification: str
    previous: int | None = None


@dataclass(frozen=True, slots=True)
class DerivativesSnapshot:
    """Leverage gauge per symbol (Binance Futures USDⓈ-M)."""

    funding_rate: dict[str, float] = field(default_factory=dict)
    """Latest *funding* rate (fraction per 8 h period)."""
    open_interest_change_24h: dict[str, float] = field(default_factory=dict)
    """Change of the *open interest* in USDT over the last 24 h (fraction)."""


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    """Metrics computed by the agent itself (**trusted data**)."""

    fear_greed: FearGreed | None = None
    derivatives: DerivativesSnapshot = field(default_factory=DerivativesSnapshot)
    btc_change_24h: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateContext:
    """TA candidate or portfolio position presented to the analyst."""

    asset: str
    symbol: str
    tier: str
    ta_score: float
    setup: str | None = None
    held: bool = False


@dataclass(frozen=True, slots=True)
class StoredNews:
    """Recorded news item (with an id for the triage) and its classification, if triaged."""

    id: int
    item: NewsItem
    relevance: float | None = None
    category: NewsCategory | None = None
    severity: Severity | None = None


@dataclass(frozen=True, slots=True)
class Triage:
    relevance: float
    category: NewsCategory
    severity: Severity
    assets: tuple[str, ...]


# ============================================================================ LLM output
class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetDraft(_Wire):
    asset: str = Field(description="Código do ativo base, ex.: SOL (deve estar na lista recebida)")
    sentiment: float = Field(description="-1 (muito negativo) a 1 (muito positivo)")
    confidence: float = Field(description="0 a 1")
    horizon: Horizon
    catalysts: list[str]
    risk_flags: list[str]
    veto: bool = Field(description="true só diante de risco grave e específico do ativo")
    rationale: str = Field(description="Resumo curto e auditável")
    sources: list[str] = Field(description="URLs que sustentam a leitura")


class MarketViewDraft(_Wire):
    market_regime: MarketRegime
    global_sentiment: float = Field(description="-1 a 1")
    exposure_multiplier: float = Field(description="0 a 1; 1 = exposição normal do perfil")
    global_risk_flags: list[str]
    assets: list[AssetDraft]


class TriageDraftItem(_Wire):
    id: int
    relevance: float = Field(description="0 (irrelevante) a 1 (muito relevante para preços)")
    category: NewsCategory
    severity: Severity
    assets: list[str]


class TriageDraft(_Wire):
    items: list[TriageDraftItem]


# ============================================================================ sanitized reading
class AssetView(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset: str
    sentiment: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    horizon: Horizon
    catalysts: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    veto: bool = False
    rationale: str = ""
    sources: tuple[str, ...] = ()


class MarketView(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: datetime
    market_regime: MarketRegime
    global_sentiment: float = Field(ge=-1, le=1)
    exposure_multiplier: float = Field(ge=0, le=1)
    global_risk_flags: tuple[str, ...] = ()
    assets: tuple[AssetView, ...] = ()

    def asset(self, code: str) -> AssetView | None:
        return next((a for a in self.assets if a.asset == code), None)
