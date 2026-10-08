"""Analyst service: news collection and the research cycle with a persisted report.

Every cycle produces a record in ``research_reports``, even when it fails (budget
exhausted, API error, invalid output): the caller then degrades to pure TA according to the
profile (``research.reading``).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anthropic
import structlog

from trade_agent import tracing
from trade_agent.persistence.research_store import ReportEntry, ResearchStore
from trade_agent.research.analyst import Analysis, MarketAnalyst
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import ResearchConfig
from trade_agent.research.digest import select_news
from trade_agent.research.llm import BudgetExceededError, ClaudeClient, LlmError, TrackingLedger
from trade_agent.research.models import (
    CandidateContext,
    MarketMetrics,
    MarketView,
    StoredNews,
)
from trade_agent.research.prompts import PROMPT_VERSION

TRIAGE_BATCH = 80

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestReport:
    collected: int
    new_items: int
    metrics: MarketMetrics
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CycleResult:
    report_id: int
    view: MarketView | None
    error: str | None
    cost_usd: Decimal
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.view is not None


class ResearchService:
    def __init__(
        self,
        *,
        store: ResearchStore,
        collector: NewsCollector,
        client: anthropic.AsyncAnthropic | None,
        config: ResearchConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._collector = collector
        self._client = client
        self._config = config
        self._clock = clock
        self.metrics = MarketMetrics()
        """Metrics of the latest collection (used in the next research cycle)."""

    @tracing.traced("research", "research.ingest")
    async def ingest(
        self, *, derivative_symbols: Sequence[str] = (), btc_change_24h: float | None = None
    ) -> IngestReport:
        collection = await self._collector.collect(
            derivative_symbols=derivative_symbols, btc_change_24h=btc_change_24h
        )
        new_items = await self._store.add_news(collection.items)
        self.metrics = collection.metrics
        log.debug(
            "research.ingested",
            collected=len(collection.items),
            new=new_items,
            errors=sorted(collection.errors),
        )
        return IngestReport(len(collection.items), new_items, collection.metrics, collection.errors)

    async def _triaged_news(
        self,
        analyst: MarketAnalyst,
        since: datetime,
        known_assets: Iterable[str],
        notes: list[str],
    ) -> list[StoredNews]:
        news = await self._store.recent_news(since)
        pending = [n for n in news if n.relevance is None][:TRIAGE_BATCH]
        if not pending:
            return news
        try:
            await self._store.set_triage(await analyst.triage(pending, known_assets))
        except BudgetExceededError:
            raise
        except LlmError as exc:  # goes on with the untriaged news
            notes.append(f"triagem indisponível: {exc}")
            return news
        return await self._store.recent_news(since)

    @tracing.traced("research", "research.cycle")
    async def run_cycle(
        self, *, trigger: str, candidates: Sequence[CandidateContext], web: bool = True
    ) -> CycleResult:
        now = self._clock()
        ledger = TrackingLedger(self._store)
        since = now - timedelta(hours=self._config.digest.lookback_hours)
        known = {c.asset for c in candidates} | set(self._config.asset_names)
        notes: list[str] = []
        analysis: Analysis | None = None
        error: str | None = None
        try:
            if self._client is None:
                raise LlmError("chave da API Claude ausente (ANTHROPIC_API_KEY)")
            # The cap applies per cycle (D-033): checked once, before the first call. A
            # check before each call let the web step (most of the cost) run and then refused
            # the reading, paying for nothing (2026-10-06, 20:03).
            llm = ClaudeClient(
                self._client, self._config, ledger, self._clock, budget_per_call=False
            )
            await llm.check_budget()
            analyst = MarketAnalyst(llm, self._config)
            news = await self._triaged_news(analyst, since, known, notes)
            analysis = await analyst.analyze(
                as_of=now,
                metrics=self.metrics,
                candidates=candidates,
                news=select_news(news, self._config.digest, now=now),
                web=web,
            )
        except LlmError as exc:
            error = str(exc)
        entry = ReportEntry(
            as_of=now,
            trigger=trigger,
            status="ok" if analysis else "failed",
            model=self._config.models.analyst,
            prompt_version=PROMPT_VERSION,
            view=analysis.view.model_dump(mode="json") if analysis else None,
            draft=analysis.draft.model_dump(mode="json") if analysis else None,
            adjustments=[*notes, *(analysis.adjustments + analysis.notes if analysis else ())],
            sources=list(analysis.findings.sources) if analysis and analysis.findings else [],
            error=error,
            cost_usd=ledger.total,
        )
        report_id = await self._store.add_report(entry)
        log.debug(
            "research.cycle",
            trigger=trigger,
            report_id=report_id,
            status=entry.status,
            cost_usd=str(ledger.total),
            candidates=len(candidates),
            notes=len(entry.adjustments),
            error=error,
        )
        return CycleResult(
            report_id=report_id,
            view=analysis.view if analysis else None,
            error=error,
            cost_usd=ledger.total,
            notes=tuple(entry.adjustments),
        )

    async def latest_view(self) -> MarketView | None:
        record = await self._store.latest_report(status="ok")
        if record is None or record.view is None:
            return None
        return MarketView.model_validate(record.view)
