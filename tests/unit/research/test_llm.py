import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest
from anthropic.types import ServerToolUsage, Usage
from structlog.testing import capture_logs

from tests.support.claude import NOW, FakeClaude, research_config
from trade_agent.research.config import WebResearchConfig
from trade_agent.research.llm import (
    BudgetExceededError,
    ClaudeClient,
    LlmError,
    LlmOutputError,
    LlmUsage,
    MemoryLedger,
    TrackingLedger,
    retry_delay,
    usage_cost,
)
from trade_agent.research.models import TriageDraft

CONFIG = research_config()
TRIAGE = {
    "items": [
        {"id": 1, "relevance": 0.8, "category": "hack", "severity": "critical", "assets": ["SOL"]}
    ]
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2.5", 2.5),
        ("-1", 0),
        ("NaN", 1),
        ("garbage", 1),
        ("Sat, 26 Sep 2026 12:00:07 GMT", 7),
        ("Sat, 26 Sep 2026 12:00:07", 1),
    ],
)
def test_retry_after_header(value: str, expected: float) -> None:
    response = httpx2.Response(
        429, headers={"retry-after": value}, request=httpx2.Request("POST", "https://example.test")
    )
    error = anthropic.RateLimitError("limited", response=response, body=None)
    assert retry_delay(error, NOW) == expected


@pytest.mark.parametrize(
    "failure", [None, httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout]
)
async def test_selective_retry_success(
    failure: type[httpx2.TransportError] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaude()
    fake.reply_json(TRIAGE)
    handler = fake._handle
    attempts = 0
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def transient(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        if request.url.path.endswith("/count_tokens"):
            return handler(request)
        attempts += 1
        if attempts == 1:
            if failure is not None:
                raise failure("pre-send", request=request)
            return httpx2.Response(
                429,
                headers={"retry-after": "3"},
                json={"error": {"type": "rate_limit_error", "message": "limited"}},
            )
        return handler(request)

    monkeypatch.setattr(fake, "_handle", transient)
    monkeypatch.setattr("trade_agent.research.llm.asyncio.sleep", sleep)
    client = fake.claude()
    await client.structured(
        purpose="triage",
        model="claude-sonnet-5",
        system="S",
        content="C",
        output=TriageDraft,
        max_tokens=2000,
    )
    assert attempts == 2 and not client.uncertain_charge
    assert sleeps == [1 if failure else 3]


async def test_retry_wait_is_cancelled_by_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClaude()

    def limited(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429, headers={"retry-after": "3600"}, json={"error": {"message": "limited"}}
        )

    monkeypatch.setattr(fake, "_handle", limited)
    client = fake.claude()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await client.structured(
                purpose="triage",
                model="claude-sonnet-5",
                system="S",
                content="C",
                output=TriageDraft,
                max_tokens=2000,
            )
    assert not client.uncertain_charge


async def test_cycle_allowance_reduces_output_and_omits_unaffordable_calls() -> None:
    fake = FakeClaude()
    fake.reply_json(TRIAGE)
    client = ClaudeClient(
        fake.client(), CONFIG, MemoryLedger(), lambda: NOW, cycle_allowance=Decimal(".1")
    )
    await client.structured(
        purpose="triage",
        model="claude-sonnet-5",
        system="S",
        content="C",
        output=TriageDraft,
        max_tokens=10000,
    )
    assert 1024 <= fake.requests[0]["max_tokens"] < 10000
    assert client.budget_notes and "reduzida" in client.budget_notes[0]
    poor = ClaudeClient(
        fake.client(), CONFIG, MemoryLedger(), lambda: NOW, cycle_allowance=Decimal(".001")
    )
    with pytest.raises(BudgetExceededError, match="saldo insuficiente"):
        await poor.structured(
            purpose="triage",
            model="claude-sonnet-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=10000,
        )
    assert len(fake.requests) == 1


async def test_bounded_calls_fail_closed_on_counting_and_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeClient(
        FakeClaude().client(), CONFIG, MemoryLedger(), lambda: NOW, cycle_allowance=Decimal(1)
    )
    request = httpx2.Request("POST", "https://api.example/messages")
    monkeypatch.setattr(
        client._client.messages,
        "count_tokens",
        AsyncMock(side_effect=anthropic.APIConnectionError(request=request)),
    )
    with pytest.raises(LlmError, match="contagem"):
        await client.structured(
            purpose="triage",
            model="claude-sonnet-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=2000,
        )
    assert bool(client.uncertain_charge) is False
    with pytest.raises(BudgetExceededError, match="ferramentas"):
        await client._fit_budget("web", {"tools": [{"type": "web_search"}]})
    monkeypatch.setattr(
        client._client.messages,
        "count_tokens",
        AsyncMock(return_value=anthropic.types.MessageTokensCount(input_tokens=1000)),
    )
    monkeypatch.setattr(
        client._client.messages,
        "create",
        AsyncMock(side_effect=anthropic.APIConnectionError(request=request)),
    )
    with pytest.raises(LlmError, match="APIConnectionError"):
        await client.structured(
            purpose="triage",
            model="claude-sonnet-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=2000,
        )
    assert client.uncertain_charge
    with pytest.raises(BudgetExceededError, match="cobrança anterior"):
        await client._fit_budget("triage", {})


def _usage(cost: str, *, hours_ago: float = 0) -> LlmUsage:
    return LlmUsage(
        "x", "claude-opus-5", 0, 0, 0, 0, 0, 0, Decimal(cost), NOW - timedelta(hours=hours_ago)
    )


@pytest.mark.parametrize(
    ("status", "uncertain"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, False),
        (413, False),
        (422, False),
        (429, False),
        (500, True),
        (504, True),
        (529, True),
    ],
)
async def test_rejections_do_not_create_uncertain_charges(status: int, uncertain: bool) -> None:
    fake = FakeClaude()
    fake.fail(status)
    if status == 429:
        fake.fail(status)
    client = ClaudeClient(
        fake.client(), CONFIG, MemoryLedger(), lambda: NOW, cycle_allowance=Decimal(1)
    )
    with pytest.raises(LlmError):
        await client.structured(
            purpose="analyst",
            model="claude-sonnet-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=2000,
        )
    assert client.uncertain_charge is uncertain
    assert len(fake.requests) == (2 if status == 429 else 1)


@pytest.mark.parametrize(
    ("failure", "uncertain"),
    [
        (httpx2.ConnectError, False),
        (httpx2.ConnectTimeout, False),
        (httpx2.PoolTimeout, False),
        (httpx2.ReadTimeout, True),
        (httpx2.WriteTimeout, True),
        (httpx2.ReadError, True),
        (httpx2.WriteError, True),
        (httpx2.RemoteProtocolError, True),
    ],
)
async def test_transport_cause_distinguishes_pre_send_from_lost_response(
    failure: type[httpx2.TransportError],
    uncertain: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClaude()
    handler = fake._handle

    def fail_after_count(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/count_tokens"):
            return handler(request)
        raise failure("simulated transport failure", request=request)

    monkeypatch.setattr(fake, "_handle", fail_after_count)
    client = ClaudeClient(
        fake.client(), CONFIG, MemoryLedger(), lambda: NOW, cycle_allowance=Decimal(1)
    )
    with pytest.raises(LlmError):
        await client.structured(
            purpose="analyst",
            model="claude-sonnet-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=2000,
        )
    assert client.uncertain_charge is uncertain


def test_usage_cost_counts_tokens_cache_and_searches() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=200_000,
        cache_read_input_tokens=1_000_000,
        server_tool_use=ServerToolUsage(web_search_requests=3, web_fetch_requests=1),
    )
    price = CONFIG.pricing.models["claude-opus-5"]
    # 5 + 2.5 + 0.2*5*1.25 + 1*5*0.1 + 3*0.01
    assert usage_cost(usage, price, CONFIG.pricing) == Decimal("9.28")
    plain = Usage(input_tokens=0, output_tokens=0)
    assert usage_cost(plain, price, CONFIG.pricing) == 0


async def test_ledgers() -> None:
    inner = MemoryLedger()
    tracking = TrackingLedger(inner)
    await tracking.record(_usage("0.5", hours_ago=30))
    await tracking.record(_usage("0.25"))
    assert tracking.total == Decimal("0.75")
    assert await tracking.spent_since(NOW - timedelta(hours=1)) == Decimal("0.25")
    assert len(inner.usages) == 2


async def test_structured_output_request_and_cost() -> None:
    fake = FakeClaude()
    fake.reply_json(
        TRIAGE, usage={"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50}
    )
    ledger = TrackingLedger(MemoryLedger())
    claude = fake.claude(ledger=ledger)
    with capture_logs() as logs:
        result = await claude.structured(
            purpose="triage",
            model="claude-sonnet-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=2000,
        )
    assert result.items[0].assets == ["SOL"]
    request, response = logs
    assert (request["event"], request["purpose"], request["model"]) == (
        "llm.request",
        "triage",
        "claude-sonnet-5",
    )
    assert (response["event"], response["stop_reason"], response["input_tokens"]) == (
        "llm.response",
        "end_turn",
        100,
    )
    assert response["cost_usd"] == "0.00031" and "content" not in response
    body = fake.requests[0]
    assert body["model"] == "claude-sonnet-5"
    assert body["system"] == [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "effort" not in body["output_config"]
    assert "tools" not in body
    (usage,) = ledger._inner.usages  # type: ignore[attr-defined]
    assert (usage.purpose, usage.cache_read_input_tokens, usage.web_search_requests) == (
        "triage",
        50,
        0,
    )
    assert ledger.total == usage.cost_usd == Decimal("0.00031")  # 100*2 + 10*10 + 50*2*0.1


async def test_structured_with_effort_and_failures() -> None:
    fake = FakeClaude()
    fake.reply_json(TRIAGE)
    fake.reply_json(TRIAGE, stop_reason="max_tokens")
    fake.reply([{"type": "text", "text": '{"items": [{"id": "x"}]}'}])
    fake.fail(500)
    ledger = TrackingLedger(MemoryLedger())
    claude = fake.claude(ledger=ledger)
    kwargs: dict[str, Any] = {
        "purpose": "analyst", "model": "claude-opus-5", "system": "S", "content": "C",
        "output": TriageDraft, "max_tokens": 2000, "effort": "high",
    }  # fmt: skip
    await claude.structured(**kwargs)
    assert fake.requests[0]["output_config"]["effort"] == "high"
    with pytest.raises(LlmOutputError, match="stop_reason=max_tokens"):
        await claude.structured(**kwargs)
    with pytest.raises(LlmOutputError, match="fora do schema"):
        await claude.structured(**kwargs)
    with pytest.raises(LlmError, match="InternalServerError"):
        await claude.structured(**kwargs)
    assert len(ledger._inner.usages) == 3  # type: ignore[attr-defined]  # invalid responses count


async def test_budget_blocks_calls_before_any_request() -> None:
    fake = FakeClaude()
    ledger = MemoryLedger()
    await ledger.record(_usage("5"))
    await ledger.record(_usage("100", hours_ago=13))  # previous day (UTC)
    claude = fake.claude(ledger=ledger)
    with pytest.raises(BudgetExceededError, match="orçamento"):
        await claude.structured(
            purpose="x",
            model="claude-opus-5",
            system="S",
            content="C",
            output=TriageDraft,
            max_tokens=1024,
        )
    assert fake.requests == []


def _search_blocks(index: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "server_tool_use",
            "id": f"srv_{index}",
            "name": "web_search",
            "input": {"query": "q"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": f"srv_{index}",
            "content": [
                {
                    "type": "web_search_result",
                    "url": "https://s.example",
                    "title": "t",
                    "encrypted_content": "e",
                }
            ],
        },
    ]


async def test_research_with_pause_turn_and_sources() -> None:
    fake = FakeClaude()
    fake.reply(_search_blocks(1), stop_reason="pause_turn")
    fetch = {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srv_2",
        "content": {
            "type": "web_fetch_result",
            "url": "https://f.example/page",
            "retrieved_at": "2026-09-26T00:00:00Z",
            "content": {
                "type": "document",
                "source": {"type": "text", "media_type": "text/plain", "data": "d"},
            },
        },
    }
    fetch_error = {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srv_3",
        "content": {"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"},
    }
    cited = {
        "type": "text",
        "text": "SOL: sem incidentes. ",
        "citations": [
            {
                "type": "web_search_result_location",
                "url": "https://c.example/1",
                "title": "t",
                "encrypted_index": "e",
                "cited_text": "c",
            },
            {
                "type": "web_search_result_location",
                "url": "https://c.example/1",
                "title": "t",
                "encrypted_index": "e",
                "cited_text": "c",
            },
        ],
    }
    fake.reply(
        [fetch, fetch_error, cited, {"type": "text", "text": "Fim."}],
        usage={
            "input_tokens": 10,
            "output_tokens": 10,
            "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
        },
    )
    ledger = TrackingLedger(MemoryLedger())
    findings = await fake.claude(ledger=ledger).research(
        purpose="research", model="claude-opus-5", system="S", content="C", effort="high"
    )
    assert findings.text == "SOL: sem incidentes. Fim."
    assert findings.sources == ("https://f.example/page", "https://c.example/1")
    first, second = fake.requests
    assert [t["type"] for t in first["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    assert first["tools"][0]["max_uses"] == 5
    assert first["output_config"] == {"effort": "high"}
    assert [m["role"] for m in second["messages"]] == ["user", "assistant"]
    assert [b["type"] for b in second["messages"][1]["content"]] == [
        "server_tool_use",
        "web_search_tool_result",
    ]
    fetches = ledger._inner.usages[1].web_fetch_requests  # type: ignore[attr-defined]
    assert fetches == 1


async def test_research_limits_and_failures() -> None:
    config = research_config(web=WebResearchConfig(max_fetches=0, max_continuations=1))
    fake = FakeClaude()
    fake.reply(_search_blocks(1), stop_reason="pause_turn")
    fake.reply(_search_blocks(2), stop_reason="pause_turn")  # uses up the resumptions
    fake.reply([{"type": "text", "text": "x"}], stop_reason="refusal")
    claude = fake.claude(config=config)
    partial = await claude.research(
        purpose="research", model="claude-opus-5", system="S", content="C"
    )
    assert partial.text == "" and partial.sources == ()
    assert [t["type"] for t in fake.requests[0]["tools"]] == ["web_search_20260209"]
    assert "output_config" not in fake.requests[0]
    assert len(fake.requests) == 2
    with pytest.raises(LlmOutputError, match="refusal"):
        await claude.research(purpose="research", model="claude-opus-5", system="S", content="C")
