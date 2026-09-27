"""API Claude simulada: o SDK ``anthropic`` real sobre ``httpx2.MockTransport``.

As respostas são enfileiradas e cada requisição é capturada (corpo JSON), sem rede.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
from pydantic import BaseModel

from trade_agent.research.config import (
    ModelPrice,
    PricingConfig,
    ResearchConfig,
    SourcesConfig,
)
from trade_agent.research.llm import ClaudeClient, MemoryLedger, TrackingLedger, UsageLedger

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

DEFAULT_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def research_config(**overrides: Any) -> ResearchConfig:
    data: dict[str, Any] = {
        "pricing": PricingConfig(
            models={
                "claude-opus-5": ModelPrice(input=Decimal(5), output=Decimal(25)),
                "claude-sonnet-5": ModelPrice(input=Decimal(2), output=Decimal(10)),
            }
        ),
        "sources": SourcesConfig(
            rss_feeds={"feed": "https://feed.example/rss"},
            binance_catalogs={48: "listagens"},
        ),
        "asset_names": {"BTC": ["bitcoin"], "SOL": ["solana"]},
    }
    data.update(overrides)
    return ResearchConfig(**data)


def message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
    model: str = "claude-opus-5",
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or DEFAULT_USAGE,
    }


class FakeClaude:
    def __init__(self) -> None:
        self._queue: list[httpx2.Response] = []
        self.requests: list[dict[str, Any]] = []

    def reply(self, content: list[dict[str, Any]], **kwargs: Any) -> None:
        self._queue.append(httpx2.Response(200, json=message(content, **kwargs)))

    def reply_json(self, payload: BaseModel | dict[str, Any], **kwargs: Any) -> None:
        text = payload.model_dump_json() if isinstance(payload, BaseModel) else json.dumps(payload)
        self.reply([{"type": "text", "text": text}], **kwargs)

    def fail(self, status: int = 500, message_text: str = "falha simulada") -> None:
        body = {"type": "error", "error": {"type": "api_error", "message": message_text}}
        self._queue.append(httpx2.Response(status, json=body))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if not self._queue:
            raise AssertionError("chamada à API Claude sem resposta enfileirada")
        return self._queue.pop(0)

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self._handle)
            ),
        )

    def claude(
        self,
        config: ResearchConfig | None = None,
        ledger: UsageLedger | None = None,
        clock: Callable[[], datetime] = lambda: NOW,
    ) -> ClaudeClient:
        return ClaudeClient(
            self.client(),
            config or research_config(),
            ledger or TrackingLedger(MemoryLedger()),
            clock,
        )
