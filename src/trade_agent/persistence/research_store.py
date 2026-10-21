"""Analyst repository: news, triage, research reports and LLM usage."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from trade_agent.persistence.db import Database
from trade_agent.persistence.models import (
    CheckpointRecord,
    LlmUsageRecord,
    NewsItemRecord,
    ResearchReportRecord,
)
from trade_agent.research.llm import BudgetExceededError, LlmUsage
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

    async def reserve_budget(
        self, day: datetime, daily: Decimal, amount: Decimal, *, now: datetime | None = None
    ) -> tuple[str, str]:
        """Atomic across the agent and manual research CLI; crashes retain reservations."""
        now = now or day
        key, token = f"research.budget.{day.date().isoformat()}", uuid4().hex
        async with self.db.session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(824172001)"))
            record = await session.get(CheckpointRecord, key)
            reserved = dict(record.value) if record else {}
            outstanding = await session.scalars(
                select(CheckpointRecord).where(CheckpointRecord.key.startswith("research.budget."))
            )
            spent = await session.scalar(
                select(func.coalesce(func.sum(LlmUsageRecord.cost_usd), 0)).where(
                    LlmUsageRecord.at >= day, LlmUsageRecord.at < day + timedelta(days=1)
                )
            )
            committed = Decimal(spent or 0) + sum(
                (
                    self._reservation(pending.key, v)[0]
                    for pending in outstanding
                    for v in pending.value.values()
                    if now < self._reservation(pending.key, v)[1] + timedelta(hours=24)
                ),
                Decimal(0),
            )
            if committed + amount > daily:
                raise BudgetExceededError("saldo diário insuficiente para reservar o ciclo")
            reserved[token] = {"amount": str(amount), "created_at": now.isoformat()}
            if record is None:
                session.add(CheckpointRecord(key=key, value=reserved))
            else:
                record.value = reserved
        return key, token

    @staticmethod
    def _reservation(key: str, value: Any) -> tuple[Decimal, datetime]:
        if isinstance(value, dict):
            return Decimal(value["amount"]), datetime.fromisoformat(value["created_at"])
        # Old checkpoints have no creation time. End of their UTC day is a conservative
        # upper bound; never expire an old reservation earlier than its actual 24h TTL.
        day = datetime.fromisoformat(key.removeprefix("research.budget.")).replace(tzinfo=UTC)
        return Decimal(value), day + timedelta(days=1)

    @staticmethod
    def _accounting_row(token: str, day: datetime, amount: Decimal, purpose: str) -> LlmUsageRecord:
        return LlmUsageRecord(
            at=day,
            purpose=purpose,
            model=f"reservation:{token}",
            cost_usd=amount,
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            web_search_requests=0,
            web_fetch_requests=0,
        )

    async def expire_budget(self, now: datetime) -> list[str]:
        """Atomically charge estimates before releasing expired reservations, once only."""
        expired: list[str] = []
        async with self.db.session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(824172001)"))
            records = await session.scalars(
                select(CheckpointRecord).where(CheckpointRecord.key.startswith("research.budget."))
            )
            for record in records:
                reserved = dict(record.value)
                for token, value in record.value.items():
                    amount, created = self._reservation(record.key, value)
                    if now < created + timedelta(hours=24):
                        continue
                    day = datetime.fromisoformat(
                        record.key.removeprefix("research.budget.")
                    ).replace(tzinfo=UTC)
                    session.add(self._accounting_row(token, day, amount, "reservation_expired"))
                    session.add(
                        CheckpointRecord(
                            key=f"research.settlement.{token}",
                            value={
                                "day": day.isoformat(),
                                "estimated_usd": str(amount),
                                "settled_usd": str(amount),
                                "expired_at": now.isoformat(),
                                "adjustments": [],
                            },
                        )
                    )
                    del reserved[token]
                    expired.append(token)
                record.value = reserved
        return expired

    async def adjust_estimate(
        self, token: str, settled_usd: Decimal, reason: str, now: datetime
    ) -> None:
        """Append a signed correction on the original UTC day; repeat totals are idempotent."""
        if not settled_usd.is_finite() or settled_usd < 0 or not reason.strip():
            raise ValueError("a finite nonnegative settled amount and an audit reason are required")
        async with self.db.session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(824172001)"))
            record = await session.get(CheckpointRecord, f"research.settlement.{token}")
            if record is None:
                raise ValueError("unknown expired reservation")
            audit = dict(record.value)
            delta = settled_usd - Decimal(audit["settled_usd"])
            if delta == 0:
                return
            session.add(
                self._accounting_row(
                    token, datetime.fromisoformat(audit["day"]), delta, "reservation_adjustment"
                )
            )
            audit["settled_usd"] = str(settled_usd)
            audit["adjustments"] = [
                *audit["adjustments"],
                {
                    "at": now.isoformat(),
                    "delta_usd": str(delta),
                    "reason": reason,
                },
            ]
            record.value = audit

    async def release_budget(self, reservation: tuple[str, str]) -> None:
        async with self.db.session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(824172001)"))
            record = await session.get(CheckpointRecord, reservation[0])
            if record is not None:
                reserved = dict(record.value)
                reserved.pop(reservation[1], None)
                record.value = reserved

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

    async def spent_since(self, start: datetime, *, until: datetime | None = None) -> Decimal:
        async with self.db.session() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(LlmUsageRecord.cost_usd), 0)).where(
                    LlmUsageRecord.at >= start,
                    *([LlmUsageRecord.at < until] if until is not None else []),
                )
            )
            return Decimal(total or 0)
