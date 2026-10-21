import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import httpx2
import pytest
import respx

from tests.support.claude import NOW, FakeClaude, research_config
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ReportEntry, ResearchStore
from trade_agent.persistence.store import Severity as EventSeverity
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import BudgetConfig, SourcesConfig
from trade_agent.research.llm import BudgetExceededError, LlmUsage
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


async def test_a_cycle_of_many_profiles_is_recorded(research_store: ResearchStore) -> None:
    # the engine's trigger grows with each profile that brought candidates
    trigger = "ciclo:" + "+".join(f"profile_{n:02d}" for n in range(12))
    stored = await research_store.add_report(_entry("ok", trigger=trigger))
    latest = await research_store.latest_report()
    assert latest is not None and latest.id == stored and latest.trigger == trigger


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


async def test_a_started_cycle_is_not_cut_short_by_the_cap(db: Database) -> None:
    """The cap applies per cycle (D-033): it is checked once, before the first call. With a
    check before each call, the web step (most of the cost) crossed the cap and the reading
    was then refused, paying for nothing (2026-10-06, 20:03)."""
    fake = FakeClaude()
    fake.reply([{"type": "text", "text": "findings"}])  # web step: US$ 0.0175, over the cap
    fake.reply_json(VIEW)
    async with httpx.AsyncClient() as http:
        tight = _service(db, fake, http, budget=BudgetConfig(daily_usd=Decimal("0.01")))
        done = await tight.run_cycle(trigger="t", candidates=CANDIDATES)
        assert done.ok and done.cost_usd > Decimal("0.01") and len(fake.requests) == 2
        refused = await tight.run_cycle(trigger="t", candidates=CANDIDATES)
    assert not refused.ok and "orçamento" in (refused.error or "")
    assert refused.cost_usd == 0 and len(fake.requests) == 2  # the next cycle calls nothing


async def test_budget_reservations_are_atomic_across_clients(db: Database) -> None:
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    outcomes = await asyncio.gather(
        *[ResearchStore(db).reserve_budget(day, Decimal(5), Decimal(1)) for _ in range(6)],
        return_exceptions=True,
    )
    assert sum(isinstance(x, BudgetExceededError) for x in outcomes) == 1
    held = [x for x in outcomes if isinstance(x, tuple)]
    assert len(held) == 5
    await ResearchStore(db).release_budget(held[0])
    assert await ResearchStore(db).reserve_budget(day, Decimal(5), Decimal(1))


async def test_reserved_cycle_omits_unbounded_web_and_alerts(db: Database) -> None:
    fake = FakeClaude()
    fake.reply_json(VIEW)
    alerts: list[str] = []
    severities: list[EventSeverity] = []

    async def notify(severity: Any, text: str) -> None:
        alerts.append(text)
        severities.append(severity)

    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http, budget=BudgetConfig(cycle_reserve_usd=Decimal(1)))
        service._notify = notify
        result = await service.run_cycle(trigger="bounded", candidates=CANDIDATES)
    assert result.ok and len(fake.requests) == 1
    assert "tools" not in fake.requests[0]
    assert alerts and "web omitida" in alerts[0]
    assert severities == [EventSeverity.INFO]
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    # Successful usage is charged, but the unused reservation is released.
    assert await ResearchStore(db).reserve_budget(day, Decimal(5), Decimal(4))


async def test_failed_billed_call_keeps_reservation(db: Database) -> None:
    fake = FakeClaude()
    fake.fail()
    budget = BudgetConfig(daily_usd=Decimal(1), cycle_reserve_usd=Decimal(1))
    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http, budget=budget)
        failed = await service.run_cycle(trigger="uncertain", candidates=CANDIDATES, web=False)
        refused = await service.run_cycle(trigger="retry", candidates=CANDIDATES, web=False)
    assert not failed.ok and any("reserva retida" in n for n in failed.notes)
    assert not refused.ok and len(fake.requests) == 1


@pytest.mark.parametrize("failure", [529, "read"])
async def test_retention_alert_includes_failure_reason(
    db: Database, failure: int | str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaude()
    handler = fake._handle

    def fail(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/count_tokens"):
            return handler(request)
        if failure == "read":
            raise httpx2.ReadTimeout("lost response", request=request)
        return httpx2.Response(529, json={"error": {"message": "overloaded"}})

    monkeypatch.setattr(fake, "_handle", fail)
    alerts: list[str] = []

    async def notify(severity: EventSeverity, text: str) -> None:
        assert severity == EventSeverity.HIGH
        alerts.append(text)

    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http, budget=BudgetConfig(cycle_reserve_usd=Decimal(1)))
        service._notify = notify
        result = await service.run_cycle(trigger="failure", candidates=CANDIDATES, web=False)
    assert not result.ok and result.error
    assert len(alerts) == 1 and result.error in alerts[0]
    assert "Reserva retida" in alerts[0] and "; motivo:" in alerts[0]


@pytest.mark.parametrize("status", [400, 401, 429])
async def test_rejected_call_releases_reservation_for_next_cycle(db: Database, status: int) -> None:
    fake = FakeClaude()
    fake.fail(status)
    if status == 429:
        fake.fail(status)
    fake.reply_json(VIEW)
    budget = BudgetConfig(daily_usd=Decimal(1), cycle_reserve_usd=Decimal(1))
    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http, budget=budget)
        rejected = await service.run_cycle(trigger="rejected", candidates=CANDIDATES, web=False)
        retried = await service.run_cycle(trigger="new-cycle", candidates=CANDIDATES, web=False)
    assert not rejected.ok and not any("reserva retida" in n for n in rejected.notes)
    assert retried.ok and len(fake.requests) == (3 if status == 429 else 2)


@pytest.mark.parametrize("triage", [False, True])
async def test_failures_still_raise_high_alerts(db: Database, triage: bool) -> None:
    fake = FakeClaude()
    fake.fail(500)
    if triage:
        await ResearchStore(db).add_news([NewsItem("rss", "Solana news", NOW)])
        fake.reply_json(VIEW)
    alerts: list[EventSeverity] = []

    async def notify(severity: EventSeverity, text: str) -> None:
        alerts.append(severity)

    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http)
        service._notify = notify
        result = await service.run_cycle(trigger="failure", candidates=CANDIDATES, web=False)
    assert result.ok is triage  # successful analysis after failed triage is still degraded
    assert alerts == [EventSeverity.HIGH]


async def test_outstanding_budget_survives_midnight(db: Database) -> None:
    store = ResearchStore(db)
    await store.release_budget(("research.budget.absent", "absent"))  # idempotent recovery
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    reservation = await store.reserve_budget(day, Decimal(1), Decimal(1), now=NOW)
    with pytest.raises(BudgetExceededError):
        await store.reserve_budget(day + timedelta(days=1), Decimal(1), Decimal(1))
    await store.release_budget(reservation)
    assert await store.reserve_budget(day + timedelta(days=1), Decimal(1), Decimal(1))


async def test_cycle_timeout_retains_inflight_reservation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClaude()
    handler = fake._handle
    started = asyncio.Event()

    async def delayed(request: httpx2.Request) -> httpx2.Response:
        if not request.url.path.endswith("/count_tokens"):
            started.set()
            await asyncio.sleep(10)
        return handler(request)

    monkeypatch.setattr(fake, "_handle", delayed)
    budget = BudgetConfig(cycle_reserve_usd=Decimal(1), cycle_timeout_s=0.2)
    async with httpx.AsyncClient() as http:
        result = await _service(db, fake, http, budget=budget).run_cycle(
            trigger="timeout",
            candidates=CANDIDATES,
            web=False,
        )
    assert started.is_set()
    assert not result.ok and result.error == "prazo total da pesquisa excedido"
    assert any("reserva retida" in note for note in result.notes)


async def test_unaffordable_triage_omits_analysis(db: Database) -> None:
    await ResearchStore(db).add_news([NewsItem("rss", "Solana news", NOW)])
    fake = FakeClaude()
    async with httpx.AsyncClient() as http:
        result = await _service(
            db,
            fake,
            http,
            budget=BudgetConfig(cycle_reserve_usd=Decimal(".001")),
        ).run_cycle(trigger="too-small", candidates=CANDIDATES, web=False)
    assert not result.ok and "saldo insuficiente" in (result.error or "")
    assert fake.requests == []


@pytest.mark.parametrize("notifications", [True, False])
async def test_restart_during_research_recovers_by_expiry(
    db: Database, monkeypatch: pytest.MonkeyPatch, notifications: bool
) -> None:
    fake = FakeClaude()
    handler = fake._handle
    started = asyncio.Event()
    alerts: list[tuple[EventSeverity, str]] = []

    async def notify(severity: EventSeverity, text: str) -> None:
        alerts.append((severity, text))

    async def interrupted(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/count_tokens"):
            return handler(request)
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(fake, "_handle", interrupted)
    budget = BudgetConfig(daily_usd=Decimal(1), cycle_reserve_usd=Decimal(1))
    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http, budget=budget)
        service._notify = notify if notifications else None
        task = asyncio.ensure_future(service.run_cycle(trigger="restart", candidates=CANDIDATES))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if notifications:
            assert len(alerts) == 1 and alerts[0][0] == EventSeverity.HIGH
            assert "Reserva retida" in alerts[0][1]
        replacement = FakeClaude()
        replacement.reply_json(VIEW)
        restarted = _service(db, replacement, http, budget=budget)
        restarted._clock = lambda: NOW + timedelta(hours=24)
        restarted._notify = notify if notifications else None
        result = await restarted.run_cycle(trigger="recovered", candidates=CANDIDATES, web=False)
    assert result.ok
    if notifications:
        assert len(alerts) == 2 and alerts[1][0] == EventSeverity.HIGH
        assert "expiradas" in alerts[1][1]
    store = ResearchStore(db)
    assert (
        await store.spent_since(NOW.replace(hour=0), until=NOW.replace(hour=0) + timedelta(days=1))
        == 1
    )


@pytest.mark.parametrize("before_send", [True, False])
async def test_transport_failure_reservation_matches_send_certainty(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    before_send: bool,
) -> None:
    fake = FakeClaude()
    fake.reply_json(VIEW)
    handler = fake._handle
    attempts = 0

    def fail_once(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        if request.url.path.endswith("/count_tokens"):
            return handler(request)
        attempts += 1
        if attempts <= (2 if before_send else 1):
            error = httpx2.ConnectTimeout if before_send else httpx2.ReadTimeout
            raise error("simulated", request=request)
        return handler(request)

    monkeypatch.setattr(fake, "_handle", fail_once)
    budget = BudgetConfig(daily_usd=Decimal(1), cycle_reserve_usd=Decimal(1))
    alerts: list[EventSeverity] = []

    async def notify(severity: EventSeverity, text: str) -> None:
        alerts.append(severity)

    async with httpx.AsyncClient() as http:
        service = _service(db, fake, http, budget=budget)
        service._notify = notify
        failed = await service.run_cycle(trigger="failure", candidates=CANDIDATES, web=False)
        subsequent = await service.run_cycle(trigger="next", candidates=CANDIDATES, web=False)
    assert not failed.ok
    assert any("reserva retida" in n for n in failed.notes) is not before_send
    assert subsequent.ok is before_send
    assert attempts == (3 if before_send else 1)
    assert alerts[0] is EventSeverity.HIGH
