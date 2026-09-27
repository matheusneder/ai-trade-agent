"""Orquestração do analista: triagem (modelo menor), pesquisa web opcional e leitura
estruturada (modelo principal), seguida das regras de segurança."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from trade_agent.research import digest
from trade_agent.research.config import ResearchConfig
from trade_agent.research.llm import (
    BudgetExceededError,
    ClaudeClient,
    LlmError,
    ResearchFindings,
)
from trade_agent.research.models import (
    CandidateContext,
    MarketMetrics,
    MarketView,
    MarketViewDraft,
    StoredNews,
    Triage,
    TriageDraft,
)
from trade_agent.research.prompts import ANALYST_SYSTEM, RESEARCH_SYSTEM, TRIAGE_SYSTEM
from trade_agent.research.safety import apply_safety


@dataclass(frozen=True, slots=True)
class Analysis:
    view: MarketView
    draft: MarketViewDraft
    adjustments: tuple[str, ...]
    findings: ResearchFindings | None
    notes: tuple[str, ...]
    """Falhas não fatais (ex.: pesquisa web indisponível; seguiu sem ela)."""


class MarketAnalyst:
    def __init__(self, llm: ClaudeClient, config: ResearchConfig) -> None:
        self._llm = llm
        self._config = config

    async def triage(
        self, news: Sequence[StoredNews], known_assets: Iterable[str]
    ) -> dict[int, Triage]:
        """Classifica as notícias com o modelo de triagem (ids desconhecidos são ignorados)."""
        if not news:
            return {}
        known = {a.upper() for a in known_assets}
        draft = await self._llm.structured(
            purpose="triage",
            model=self._config.models.triage,
            system=TRIAGE_SYSTEM,
            content=digest.triage_input(news, known),
            output=TriageDraft,
            max_tokens=self._config.models.triage_max_tokens,
        )
        ids = {n.id for n in news}
        return {
            item.id: Triage(
                relevance=min(max(item.relevance, 0.0), 1.0),
                category=item.category,
                severity=item.severity,
                assets=tuple(sorted({a.upper() for a in item.assets} & known)),
            )
            for item in draft.items
            if item.id in ids
        }

    async def analyze(
        self,
        *,
        as_of: datetime,
        metrics: MarketMetrics,
        candidates: Sequence[CandidateContext],
        news: Sequence[StoredNews],
        web: bool,
    ) -> Analysis:
        models = self._config.models
        notes: list[str] = []
        findings: ResearchFindings | None = None
        if web and self._config.web.enabled and candidates:
            try:
                findings = await self._llm.research(
                    purpose="research",
                    model=models.analyst,
                    system=RESEARCH_SYSTEM,
                    content=digest.research_input(as_of=as_of, candidates=candidates, news=news),
                    effort=models.effort,
                )
            except BudgetExceededError:
                raise
            except LlmError as exc:
                notes.append(f"pesquisa web indisponível: {exc}")
        draft = await self._llm.structured(
            purpose="analyst",
            model=models.analyst,
            system=ANALYST_SYSTEM,
            content=digest.analyst_input(
                as_of=as_of,
                metrics=metrics,
                candidates=candidates,
                news=news,
                findings=findings,
                config=self._config.digest,
            ),
            output=MarketViewDraft,
            max_tokens=models.max_tokens,
            effort=models.effort,
        )
        result = apply_safety(
            draft,
            allowed_assets={c.asset for c in candidates},
            as_of=as_of,
            config=self._config.safety,
        )
        return Analysis(result.view, draft, result.adjustments, findings, tuple(notes))
