from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from tests.support.claude import NOW, FakeClaude, research_config
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ReportEntry, ResearchStore
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import BudgetConfig, SourcesConfig
from trade_agent.research.llm import LlmUsage
from trade_agent.research.models import (
    CandidateContext,
    NewsCategory,
    NewsItem,
    Severity,
    Triage,
)
from trade_agent.research.service import ResearchService
from trade_agent.research.tagging import AssetTagger

CANDIDATES = [CandidateContext("SOL", "SOLUSDT", "large", 0.6)]
FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Solana validators patch bug</title><link>https://feed.example/1</link>
<pubDate>Sat, 26 Sep 2026 11:00:00 +0000</pubDate></item>
<item><title>Bitcoin steady</title><link>https://feed.example/2</link>
<pubDate>Sat, 26 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

VIEW = {
    "market_regime": "neutral",
    "global_sentiment": 0.1,
    "exposure_multiplier": 0.9,
    "global_risk_flags": [],
    "assets": [
        {
            "asset": "SOL", "sentiment": -0.2, "confidence": 0.6, "horizon": "days",
            "catalysts": [], "risk_flags": ["bug corrigido"], "veto": False,
            "rationale": "r", "sources": [],
        }
    ],
}  # fmt: skip


@pytest.fixture
def research_store(db: Database) -> ResearchStore:
    return ResearchStore(db)


def _usage(cost: str, hours_ago: float = 0) -> LlmUsage:
    return LlmUsage(
        "analyst",
        "claude-opus-5",
        10,
        5,
        1,
        2,
        3,
        4,
        Decimal(cost),
        NOW - timedelta(hours=hours_ago),
    )


def _entry(status: str, **overrides: Any) -> ReportEntry:
    data: dict[str, Any] = {
        "as_of": NOW, "trigger": "manual", "status": status, "model": "claude-opus-5",
        "prompt_version": "v1", "view": None, "draft": None, "adjustments": ["a"],
        "sources": [], "error": None, "cost_usd": Decimal("0.1"),
    }  # fmt: skip
    data.update(overrides)
    return ReportEntry(**data)


async def test_news_dedupe_window_and_triage(research_store: ResearchStore) -> None:
    items = [
        NewsItem("rss", "a", NOW - timedelta(hours=1), url="https://a.example", assets=("SOL",)),
        NewsItem("rss", "b", NOW - timedelta(hours=2)),
        NewsItem("rss", "antiga", NOW - timedelta(hours=30)),
    ]
    assert await research_store.add_news(items) == 3
    assert await research_store.add_news(items[:1]) == 0
    assert await research_store.add_news([]) == 0
    recent = await research_store.recent_news(NOW - timedelta(hours=24))
    assert [n.item.title for n in recent] == ["a", "b"]
    assert recent[0].item.assets == ("SOL",) and recent[0].item.url == "https://a.example"
    assert recent[0].relevance is None and recent[0].category is None
    triage = Triage(0.7, NewsCategory.HACK, Severity.CRITICAL, ("BTC", "SOL", "BTC"))
    await research_store.set_triage({recent[1].id: triage})
    triaged = (await research_store.recent_news(NOW - timedelta(hours=24)))[1]
    assert (triaged.relevance, triaged.category, triaged.severity) == (
        0.7,
        NewsCategory.HACK,
        Severity.CRITICAL,
    )
    assert triaged.item.assets == ("BTC", "SOL")


async def test_reports_and_usage_ledger(research_store: ResearchStore) -> None:
    assert await research_store.latest_report() is None
    failed = await research_store.add_report(_entry("failed", error="boom"))
    ok = await research_store.add_report(_entry("ok", view={"x": 1}, draft={"y": 2}))
    await research_store.add_report(_entry("failed"))
    latest_ok = await research_store.latest_report(status="ok")
    assert latest_ok is not None and latest_ok.id == ok and latest_ok.view == {"x": 1}
    latest = await research_store.latest_report()
    assert latest is not None and latest.id > ok > failed
    assert await research_store.spent_since(NOW - timedelta(hours=1)) == 0
    await research_store.record(_usage("0.25"))
    await research_store.record(_usage("1.5", hours_ago=30))
    assert await research_store.spent_since(NOW - timedelta(hours=1)) == Decimal("0.25")


def _service(
    db: Database, fake: FakeClaude | None, http: httpx.AsyncClient, **config: Any
) -> ResearchService:
    settings = research_config(
        sources=SourcesConfig(rss_feeds={"feed": "https://feed.example/rss"}, fear_greed=False),
        **config,
    )
    return ResearchService(
        store=ResearchStore(db),
        collector=NewsCollector(
            http, settings.sources, AssetTagger(["SOL", "BTC"], {"SOL": ["solana"]}), lambda: NOW
        ),
        client=fake.client() if fake else None,
        config=settings,
        clock=lambda: NOW,
    )


async def test_ingest_cycle_and_latest_view(db: Database) -> None:
    fake = FakeClaude()
    relevant = {"id": 1, "relevance": 0.9, "category": "project", "severity": "medium"}
    noise = {"id": 2, "relevance": 0.1, "category": "market", "severity": "low"}
    fake.reply_json(
        {"items": [{**relevant, "assets": ["SOL"]}, {**noise, "assets": ["BTC"]}]},
        model="claude-sonnet-5",
    )
    fake.reply_json(VIEW)
    fake.reply_json(VIEW)
    with respx.mock() as router:
        router.get("https://feed.example/rss").respond(200, content=FEED)
        async with httpx.AsyncClient() as http:
            service = _service(db, fake, http)
            ingest = await service.ingest(btc_change_24h=0.01)
    assert (ingest.collected, ingest.new_items, dict(ingest.errors)) == (2, 2, {})
    assert service.metrics.btc_change_24h == 0.01
    assert await service.latest_view() is None

    result = await service.run_cycle(trigger="manual", candidates=CANDIDATES, web=False)
    assert result.ok and result.error is None
    assert result.cost_usd > 0
    assert result.view is not None and result.view.asset("SOL") is not None
    assert await service.latest_view() == result.view
    triage_request, analyst_request = fake.requests
    assert triage_request["model"] == "claude-sonnet-5"
    content = analyst_request["messages"][0]["content"]
    assert "Solana validators" in content and "Bitcoin steady" not in content  # irrelevant
    assert "BTC 24h: +1.00%" in content

    again = await service.run_cycle(trigger="scheduled", candidates=CANDIDATES, web=False)
    assert again.ok and len(fake.requests) == 3  # nothing waiting for triage


async def test_cycle_failures_are_recorded(db: Database) -> None:
    store = ResearchStore(db)
    async with httpx.AsyncClient() as http:
        no_key = await _service(db, None, http).run_cycle(trigger="t", candidates=CANDIDATES)
        assert not no_key.ok and "ANTHROPIC_API_KEY" in (no_key.error or "")

        await store.add_news([NewsItem("rss", "Solana news", NOW - timedelta(hours=1))])
        fake = FakeClaude()
        fake.fail(500)  # triage unavailable...
        fake.reply_json(VIEW)  # ...but the reading comes out
        degraded = await _service(db, fake, http).run_cycle(
            trigger="t", candidates=CANDIDATES, web=False
        )
        assert degraded.ok
        assert degraded.notes[0].startswith("triagem indisponível")

        fake.fail(500)  # triage pending again, and it fails
        fake.reply([{"type": "text", "text": "{}"}])  # reading outside the schema
        invalid = await _service(db, fake, http).run_cycle(
            trigger="t", candidates=CANDIDATES, web=False
        )
        assert not invalid.ok and "fora do schema" in (invalid.error or "")

        broke = _service(db, fake, http, budget=BudgetConfig(daily_usd=Decimal("0.001")))
        exhausted = await broke.run_cycle(trigger="t", candidates=CANDIDATES, web=False)
        assert not exhausted.ok and "orçamento" in (exhausted.error or "")

    latest = await store.latest_report()
    assert latest is not None and latest.status == "failed" and latest.cost_usd == 0
    assert (await store.latest_report(status="ok")) is not None
