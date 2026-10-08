"""Analyst repository: news, triage, research reports and LLM usage."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from trade_agent.persistence.db import Database
from trade_agent.persistence.models import LlmUsageRecord, NewsItemRecord, ResearchReportRecord
from trade_agent.research.llm import LlmUsage
from trade_agent.research.models import NewsCategory, NewsItem, Severity, StoredNews, Triage


@dataclass(frozen=True, slots=True)
class ReportEntry:
    as_of: datetime
    trigger: str
    status: str
    model: str
    prompt_version: str
    view: Mapping[str, Any] | None
    draft: Mapping[str, Any] | None
    adjustments: Sequence[str]
    sources: Sequence[str]
    error: str | None
    cost_usd: Decimal


def _to_news(record: NewsItemRecord) -> StoredNews:
    return StoredNews(
        id=record.id,
        item=NewsItem(
            source=record.source,
            title=record.title,
            url=record.url,
            published_at=record.published_at,
            summary=record.summary,
            assets=tuple(record.assets),
        ),
        relevance=record.relevance,
        category=NewsCategory(record.category) if record.category else None,
        severity=Severity(record.severity) if record.severity else None,
    )


class ResearchStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ================================================================== news
    async def add_news(self, items: Iterable[NewsItem]) -> int:
        """Records the new news items (deduplicated by ``dedupe_key``); returns how many."""
        rows = [
            {
                "dedupe_key": i.dedupe_key,
                "source": i.source,
                "title": i.title,
                "url": i.url,
                "summary": i.summary,
                "published_at": i.published_at,
                "assets": list(i.assets),
            }
            for i in items
        ]
        if not rows:
            return 0
        statement = (
            insert(NewsItemRecord)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(NewsItemRecord.id)
        )
        async with self.db.session() as session:
            return len((await session.execute(statement)).all())

    async def recent_news(self, since: datetime, *, limit: int = 500) -> list[StoredNews]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(NewsItemRecord)
                .where(NewsItemRecord.published_at >= since)
                .order_by(NewsItemRecord.published_at.desc(), NewsItemRecord.id.desc())
                .limit(limit)
            )
            return [_to_news(r) for r in records]

    async def set_triage(self, triage: Mapping[int, Triage]) -> None:
        async with self.db.session() as session:
            for news_id, t in triage.items():
                await session.execute(
                    update(NewsItemRecord)
                    .where(NewsItemRecord.id == news_id)
                    .values(
                        relevance=t.relevance,
                        category=t.category.value,
                        severity=t.severity.value,
                        assets=sorted(set(t.assets)),
                    )
                )

    # ================================================================== reports and usage
    async def add_report(self, entry: ReportEntry) -> int:
        record = ResearchReportRecord(
            as_of=entry.as_of,
            trigger=entry.trigger,
            status=entry.status,
            model=entry.model,
            prompt_version=entry.prompt_version,
            view=dict(entry.view) if entry.view is not None else None,
            draft=dict(entry.draft) if entry.draft is not None else None,
            adjustments=list(entry.adjustments),
            sources=list(entry.sources),
            error=entry.error,
            cost_usd=entry.cost_usd,
        )
        async with self.db.session() as session:
            session.add(record)
            await session.flush()
            return record.id

    async def latest_report(self, *, status: str | None = None) -> ResearchReportRecord | None:
        query = select(ResearchReportRecord).order_by(ResearchReportRecord.id.desc()).limit(1)
        if status is not None:
            query = query.where(ResearchReportRecord.status == status)
        async with self.db.session() as session:
            return (await session.scalars(query)).first()

    async def record(self, usage: LlmUsage) -> None:
        async with self.db.session() as session:
            session.add(
                LlmUsageRecord(
                    at=usage.at,
                    purpose=usage.purpose,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                    web_search_requests=usage.web_search_requests,
                    web_fetch_requests=usage.web_fetch_requests,
                    cost_usd=usage.cost_usd,
                )
            )

    async def spent_since(self, start: datetime) -> Decimal:
        async with self.db.session() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(LlmUsageRecord.cost_usd), 0)).where(
                    LlmUsageRecord.at >= start
                )
            )
            return Decimal(total or 0)
