"""Contratos do analista: itens de notícia, métricas de mercado e ``MarketView`` (doc 03, §7.2).

Há dois níveis de modelo para a saída do LLM:

* ``*Draft``: o formato pedido ao modelo (*structured outputs*), **sem** restrições
  numéricas. Valores fora da faixa não invalidam a resposta inteira; quem os trunca é o
  código (``research.safety``), como manda a regra de segurança;
* ``MarketView``/``AssetView``: a leitura já saneada, com faixas garantidas.
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


# ============================================================================ entradas
@dataclass(frozen=True, slots=True)
class NewsItem:
    """Notícia ou anúncio coletado (conteúdo externo: **dado não confiável**)."""

    source: str
    title: str
    published_at: datetime
    url: str | None = None
    summary: str = ""
    assets: tuple[str, ...] = ()

    @property
    def dedupe_key(self) -> str:
        """Chave de deduplicação: fonte + URL (ou título normalizado, sem URL)."""
        basis = self.url or " ".join(self.title.lower().split())
        return hashlib.sha256(f"{self.source}|{basis}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class FearGreed:
    value: int
    classification: str
    previous: int | None = None


@dataclass(frozen=True, slots=True)
class DerivativesSnapshot:
    """Termômetro de alavancagem por símbolo (Binance Futures USDⓈ-M)."""

    funding_rate: dict[str, float] = field(default_factory=dict)
    """Última taxa de *funding* (fração por período de 8h)."""
    open_interest_change_24h: dict[str, float] = field(default_factory=dict)
    """Variação do *open interest* em USDT nas últimas 24h (fração)."""


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    """Métricas calculadas pelo próprio agente (**dado confiável**)."""

    fear_greed: FearGreed | None = None
    derivatives: DerivativesSnapshot = field(default_factory=DerivativesSnapshot)
    btc_change_24h: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateContext:
    """Candidato do TA ou posição em carteira apresentado ao analista."""

    asset: str
    symbol: str
    tier: str
    ta_score: float
    setup: str | None = None
    held: bool = False


@dataclass(frozen=True, slots=True)
class StoredNews:
    """Notícia gravada (com id para a triagem) e a classificação, se já triada."""

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


# ============================================================================ saída do LLM
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


# ============================================================================ leitura saneada
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
