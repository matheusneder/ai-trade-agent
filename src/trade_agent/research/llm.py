"""Chamadas à API Claude: saída estruturada, pesquisa com busca web, custo e orçamento.

* **Orçamento:** antes de cada chamada, o gasto do dia (UTC) é comparado ao teto
  ``budget.daily_usd``; atingido o teto, nada é chamado (``BudgetExceededError``).
* **Custo:** cada resposta é registrada no ``UsageLedger`` (tokens, cache, buscas e US$)
  **antes** de validar a saída, para que respostas inválidas também sejam contabilizadas.
* **Saída estruturada:** ``output_config.format`` com o schema gerado pelo SDK
  (``transform_schema``) e validação local com pydantic.
* **Pesquisa:** ferramentas de servidor ``web_search``/``web_fetch`` com ``max_uses``;
  ``pause_turn`` é retomado até ``max_continuations`` vezes.
* O *system prompt* é marcado para *prompt caching* (prefixo estável).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import anthropic
import structlog
from anthropic.types import Message, Usage
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ValidationError

from trade_agent import tracing
from trade_agent.research.config import ModelPrice, PricingConfig, ResearchConfig

log = structlog.get_logger(__name__)

MILLION = Decimal(1_000_000)


class LlmError(Exception):
    """Falha do analista; o chamador degrada para TA pura."""


class BudgetExceededError(LlmError):
    pass


class LlmOutputError(LlmError):
    """Resposta sem saída utilizável (recusa, truncamento, JSON fora do schema)."""


@dataclass(frozen=True, slots=True)
class LlmUsage:
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    web_search_requests: int
    web_fetch_requests: int
    cost_usd: Decimal
    at: datetime


class UsageLedger(Protocol):
    async def spent_since(self, start: datetime) -> Decimal: ...

    async def record(self, usage: LlmUsage) -> None: ...


class MemoryLedger:
    """Livro-caixa em memória (avaliação offline e testes)."""

    def __init__(self) -> None:
        self.usages: list[LlmUsage] = []

    async def spent_since(self, start: datetime) -> Decimal:
        return sum((u.cost_usd for u in self.usages if u.at >= start), Decimal(0))

    async def record(self, usage: LlmUsage) -> None:
        self.usages.append(usage)


class TrackingLedger:
    """Repassa ao livro-caixa principal e soma o custo das chamadas de um ciclo."""

    def __init__(self, inner: UsageLedger) -> None:
        self._inner = inner
        self.total = Decimal(0)

    async def spent_since(self, start: datetime) -> Decimal:
        return await self._inner.spent_since(start)

    async def record(self, usage: LlmUsage) -> None:
        self.total += usage.cost_usd
        await self._inner.record(usage)


def usage_cost(usage: Usage, price: ModelPrice, pricing: PricingConfig) -> Decimal:
    """Custo em US$ de uma resposta (tokens de *thinking* já estão em ``output_tokens``)."""
    cached_write = Decimal(usage.cache_creation_input_tokens or 0)
    cached_read = Decimal(usage.cache_read_input_tokens or 0)
    searches = usage.server_tool_use.web_search_requests if usage.server_tool_use else 0
    return (
        Decimal(usage.input_tokens) * price.input
        + Decimal(usage.output_tokens) * price.output
        + cached_write * price.input * pricing.cache_write_multiplier
        + cached_read * price.input * pricing.cache_read_multiplier
    ) / MILLION + Decimal(searches) * pricing.web_search_per_1000 / 1000


@dataclass(frozen=True, slots=True)
class ResearchFindings:
    text: str
    sources: tuple[str, ...]
    """URLs citadas no texto ou lidas com ``web_fetch``."""


def _findings(blocks: Sequence[Any]) -> ResearchFindings:
    texts: list[str] = []
    sources: list[str] = []
    for block in blocks:
        if block.type == "text":
            texts.append(block.text)
            sources.extend(url for c in block.citations or () if (url := getattr(c, "url", None)))
        elif block.type == "web_fetch_tool_result":
            url = getattr(block.content, "url", None)
            if url:
                sources.append(url)
    return ResearchFindings("".join(texts).strip(), tuple(dict.fromkeys(sources)))


class ClaudeClient:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        config: ResearchConfig,
        ledger: UsageLedger,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._config = config
        self._ledger = ledger
        self._clock = clock

    async def _check_budget(self) -> None:
        now = self._clock()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = await self._ledger.spent_since(day_start)
        if spent >= self._config.budget.daily_usd:
            raise BudgetExceededError(
                f"orçamento diário do LLM atingido (US$ {spent:.2f} de "
                f"US$ {self._config.budget.daily_usd})"
            )

    async def _record(self, purpose: str, model: str, response: Message) -> None:
        usage = response.usage
        server = usage.server_tool_use
        record = LlmUsage(
            purpose=purpose,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            web_search_requests=server.web_search_requests if server else 0,
            web_fetch_requests=(server.web_fetch_requests or 0) if server else 0,
            cost_usd=usage_cost(usage, self._config.pricing.models[model], self._config.pricing),
            at=self._clock(),
        )
        await self._ledger.record(record)
        current = trace.get_current_span()
        current.set_attribute("gen_ai.response.finish_reasons", [str(response.stop_reason)])
        current.set_attribute("gen_ai.usage.input_tokens", record.input_tokens)
        current.set_attribute("gen_ai.usage.output_tokens", record.output_tokens)
        current.set_attribute("trade_agent.cache_read_input_tokens", record.cache_read_input_tokens)
        current.set_attribute("trade_agent.web_searches", record.web_search_requests)
        current.set_attribute("trade_agent.cost_usd", str(record.cost_usd))
        log.debug(
            "llm.response",
            purpose=purpose,
            model=model,
            stop_reason=response.stop_reason,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cache_read_input_tokens=record.cache_read_input_tokens,
            web_searches=record.web_search_requests,
            cost_usd=str(record.cost_usd),
        )

    def _system(self, text: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    async def _create(self, purpose: str, **params: Any) -> Message:
        model = params["model"]
        attributes: dict[str, tracing.AttributeValue] = {
            "gen_ai.provider.name": "anthropic",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "gen_ai.request.max_tokens": params.get("max_tokens") or 0,
            "peer.service": "anthropic",
            "trade_agent.purpose": purpose,
        }
        with tracing.span("llm", f"chat {model}", kind=SpanKind.CLIENT, attributes=attributes):
            await self._check_budget()
            log.debug(
                "llm.request",
                purpose=purpose,
                model=model,
                max_tokens=params.get("max_tokens"),
                tools=[t["type"] for t in params.get("tools", [])],
                turns=len(params.get("messages", [])),
            )
            try:
                response: Message = await self._client.messages.create(
                    timeout=self._config.models.timeout_s, **params
                )
            except anthropic.APIError as exc:
                raise LlmError(f"{purpose}: {type(exc).__name__}: {exc}") from exc
            await self._record(purpose, model, response)
            return response

    async def structured[T: BaseModel](
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        content: str,
        output: type[T],
        max_tokens: int,
        effort: str | None = None,
    ) -> T:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(output)}
        }
        if effort is not None:
            output_config["effort"] = effort
        response = await self._create(
            purpose,
            model=model,
            max_tokens=max_tokens,
            system=self._system(system),
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
        )
        if response.stop_reason != "end_turn":
            raise LlmOutputError(f"{purpose}: stop_reason={response.stop_reason}")
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            return output.model_validate_json(text)
        except ValidationError as exc:
            raise LlmOutputError(f"{purpose}: saída fora do schema: {exc}") from exc

    async def research(
        self, *, purpose: str, model: str, system: str, content: str, effort: str | None = None
    ) -> ResearchFindings:
        """Pesquisa com busca web (texto livre + fontes)."""
        web = self._config.web
        tools: list[dict[str, Any]] = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": web.max_searches}
        ]
        if web.max_fetches:
            tools.append(
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": web.max_fetches}
            )
        extra: dict[str, Any] = {"output_config": {"effort": effort}} if effort else {}
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        blocks: list[Any] = []
        for _ in range(web.max_continuations + 1):
            response = await self._create(
                purpose,
                model=model,
                max_tokens=self._config.models.max_tokens,
                system=self._system(system),
                messages=messages,
                tools=tools,
                **extra,
            )
            blocks.extend(response.content)
            if response.stop_reason != "pause_turn":
                break
            # retoma o laço do servidor: reenviar a pergunta e a resposta parcial
            messages = [
                {"role": "user", "content": content},
                {"role": "assistant", "content": blocks},
            ]
        if response.stop_reason not in {"end_turn", "pause_turn"}:
            raise LlmOutputError(f"{purpose}: stop_reason={response.stop_reason}")
        return _findings(blocks)
