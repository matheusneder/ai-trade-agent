"""Analyst configuration (``config/research.yaml``), validated by schema."""

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelsConfig(_Strict):
    analyst: str = "claude-opus-5"
    triage: str = "claude-sonnet-5"
    effort: Effort = "high"
    max_tokens: int = Field(default=16_000, ge=1024, le=20_000)
    triage_max_tokens: int = Field(default=8_000, ge=1024, le=20_000)
    timeout_s: float = Field(default=300, gt=0)


class ModelPrice(_Strict):
    """US$ per million tokens."""

    input: Decimal = Field(gt=0)
    output: Decimal = Field(gt=0)


class PricingConfig(_Strict):
    models: dict[str, ModelPrice]
    cache_write_multiplier: Decimal = Decimal("1.25")
    cache_read_multiplier: Decimal = Decimal("0.1")
    web_search_per_1000: Decimal = Decimal(10)


class BudgetConfig(_Strict):
    daily_usd: Decimal = Field(default=Decimal(5), gt=0)
    """Daily (UTC) cap on LLM spending; once reached, the analyst is not called."""


class WebResearchConfig(_Strict):
    enabled: bool = True
    max_searches: int = Field(default=5, ge=1, le=20)
    max_fetches: int = Field(default=3, ge=0, le=20)
    max_continuations: int = Field(default=3, ge=0, le=10)
    """Resumptions after ``pause_turn`` (the server's tool loop)."""


class SourcesConfig(_Strict):
    rss_feeds: dict[str, str] = Field(default_factory=dict)
    """Source name → RSS/Atom feed URL."""
    binance_catalogs: dict[int, str] = Field(default_factory=dict)
    """Binance announcement catalogs (id → label)."""
    fear_greed: bool = True
    derivatives: bool = True
    derivatives_max_symbols: int = Field(default=12, ge=0, le=50)
    timeout_s: float = Field(default=15, gt=0)


class DigestConfig(_Strict):
    lookback_hours: int = Field(default=12, ge=1, le=72)
    max_items: int = Field(default=60, ge=1, le=300)
    summary_chars: int = Field(default=280, ge=0, le=2000)
    min_relevance: float = Field(default=0.3, ge=0, le=1)
    """Minimum relevance (triage) for a news item to make it into the digest."""


class SafetyConfig(_Strict):
    bullish_threshold: float = Field(default=0.5, ge=0, le=1)
    min_bullish_sources: int = Field(default=2, ge=0)
    max_text_chars: int = Field(default=400, ge=20)
    max_list_items: int = Field(default=5, ge=1)
    max_view_age_hours: float = Field(default=8, gt=0)
    """A reading older than this is treated as a failure (degradation)."""


class ResearchConfig(_Strict):
    models: ModelsConfig = ModelsConfig()
    pricing: PricingConfig
    budget: BudgetConfig = BudgetConfig()
    web: WebResearchConfig = WebResearchConfig()
    sources: SourcesConfig = SourcesConfig()
    digest: DigestConfig = DigestConfig()
    safety: SafetyConfig = SafetyConfig()
    asset_names: dict[str, list[str]] = Field(default_factory=dict)
    """Names used to tag the assets mentioned in the news (besides the code)."""

    @model_validator(mode="after")
    def _priced_models(self) -> "ResearchConfig":
        missing = {self.models.analyst, self.models.triage} - set(self.pricing.models)
        if missing:
            raise ValueError(f"modelos sem preço em pricing.models: {sorted(missing)}")
        return self


def load_research_config(path: Path | str) -> ResearchConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ResearchConfig.model_validate(data)
