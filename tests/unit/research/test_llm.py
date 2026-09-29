from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from anthropic.types import ServerToolUsage, Usage
from structlog.testing import capture_logs

from tests.support.claude import NOW, FakeClaude, research_config
from trade_agent.research.config import WebResearchConfig
from trade_agent.research.llm import (
    BudgetExceededError,
    LlmError,
    LlmOutputError,
    LlmUsage,
    MemoryLedger,
    TrackingLedger,
    usage_cost,
)
from trade_agent.research.models import TriageDraft

CONFIG = research_config()
TRIAGE = {
    "items": [
        {"id": 1, "relevance": 0.8, "category": "hack", "severity": "critical", "assets": ["SOL"]}
    ]
}


def _usage(cost: str, *, hours_ago: float = 0) -> LlmUsage:
    return LlmUsage(
        "x", "claude-opus-5", 0, 0, 0, 0, 0, 0, Decimal(cost), NOW - timedelta(hours=hours_ago)
    )


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
    assert len(ledger._inner.usages) == 3  # type: ignore[attr-defined]  # respostas inválidas contam


async def test_budget_blocks_calls_before_any_request() -> None:
    fake = FakeClaude()
    ledger = MemoryLedger()
    await ledger.record(_usage("5"))
    await ledger.record(_usage("100", hours_ago=13))  # dia anterior (UTC)
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
    fake.reply(_search_blocks(2), stop_reason="pause_turn")  # esgota as retomadas
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
