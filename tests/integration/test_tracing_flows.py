"""Spans of each component against simulated Binance, PostgreSQL, fake Claude and Telegram.

Beyond the hierarchy (who is whose parent), the tests make sure no secret reaches the
spans: Binance signature and key, Telegram token, LLM content, SQL values.
"""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
import respx
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import text

from tests.support.claude import FakeClaude
from tests.support.fake_binance import FakeBinance
from tests.support.risk import CONDITIONS
from tests.support.tracing import Recorded, component
from trade_agent import tracing
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceConnectionError, BinanceRejectedError
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.notify.commands import CommandCenter
from trade_agent.notify.notifier import TelegramNotifier
from trade_agent.notify.telegram import TelegramBot
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Severity, Store
from trade_agent.reconcile.reconciler import Reconciler
from trade_agent.research.models import TriageDraft
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.state import StateStore
from trade_agent.runtime import AgentRuntime


# ============================================================================ Binance
async def test_binance_calls_are_client_spans_without_secrets(
    spans: Recorded, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    with tracing.span("risk", "risk.snapshot"):
        await api.rest.sync_time()
        await api.account()  # signed: timestamp, recvWindow and signature in the query
        with pytest.raises(BinanceRejectedError):
            await api.klines("NAOEXISTE", "1m", limit=5)
    *_, time_span = spans.named("GET /api/v3/time")  # one per clock sample
    account = spans.one("GET /api/v3/account")
    rejected = spans.one("GET /api/v3/klines")
    for span in (*spans.named("GET /api/v3/time"), account, rejected):
        assert component(span) == "exchange" and span.kind is SpanKind.CLIENT
        assert spans.parent(span) == spans.one("risk.snapshot")
    attributes = account.attributes or {}
    assert attributes["url.path"] == "/api/v3/account"
    assert attributes["http.response.status_code"] == 200
    assert attributes["peer.service"] == "binance" and attributes["trade_agent.signed"] is True
    assert (time_span.attributes or {})["trade_agent.signed"] is False
    assert rejected.status.status_code is StatusCode.ERROR
    assert (rejected.attributes or {})["http.response.status_code"] == 400
    text_ = spans.attribute_text()
    assert "signature" not in text_ and "timestamp=" not in text_ and "recvWindow" not in text_
    assert "fake-key" not in text_ and "NAOEXISTE" not in text_  # not even the public query


async def test_binance_connection_failures_are_marked(spans: Recorded) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusada", request=request)

    http = httpx.AsyncClient(base_url="https://fake.binance", transport=httpx.MockTransport(refuse))
    async with BinanceRestClient("https://fake.binance", http_client=http) as rest:
        with pytest.raises(BinanceConnectionError):
            await rest.public("GET", "/api/v3/ping")
    await http.aclose()
    failed = spans.one("GET /api/v3/ping")
    assert failed.status.status_code is StatusCode.ERROR
    assert (failed.attributes or {})["error.type"] == "ConnectError"
    assert (failed.attributes or {})["server.address"] == "fake.binance"


# ============================================================================ PostgreSQL
async def test_sql_statements_are_children_of_the_task(
    spans: Recorded, db: Database, store: Store
) -> None:
    with tracing.span("runtime", "job check_risk"):
        await store.record_event("teste", Severity.INFO, {"nota": "valor-que-nao-vaza"})
        with pytest.raises(Exception, match="tabela_inexistente"):
            async with db.engine.connect() as conn:
                await conn.execute(text("SELECT * FROM tabela_inexistente"))
    job = spans.one("job check_risk")
    insert = spans.one("INSERT events")
    assert component(insert) == "db" and insert.kind is SpanKind.CLIENT
    assert spans.parent(insert) == job  # the context crosses SQLAlchemy's greenlet
    attributes = insert.attributes or {}
    assert attributes["db.system.name"] == "postgresql"
    assert attributes["db.collection.name"] == "events"
    assert "$1" in str(attributes["db.query.text"])
    assert "valor-que-nao-vaza" not in spans.attribute_text()
    failed = spans.one("SELECT tabela_inexistente")
    assert failed.status.status_code is StatusCode.ERROR
    assert (failed.attributes or {})["error.type"] == "ProgrammingError"
    assert (failed.attributes or {})["db.response.status_code"] == "42P01"  # undefined_table
    assert ("runtime", "db") in spans.edges()


# ============================================================================ LLM
async def test_llm_span_has_usage_but_no_content(spans: Recorded) -> None:
    fake = FakeClaude()
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 50,
    }
    fake.reply_json(TriageDraft(items=[]), usage=usage)
    with tracing.span("research", "analyst.triage"):
        await fake.claude().structured(
            purpose="triage",
            model="claude-sonnet-5",
            system="PROMPT-SECRETO",
            content="NOTICIA-SIGILOSA",
            output=TriageDraft,
            max_tokens=2000,
        )
    chat = spans.one("chat claude-sonnet-5")
    assert component(chat) == "llm" and chat.kind is SpanKind.CLIENT
    attributes = chat.attributes or {}
    assert attributes["gen_ai.provider.name"] == "anthropic"
    # GenAI convention: the input includes the cache, which Anthropic reports apart
    assert attributes["gen_ai.usage.input_tokens"] == 450
    assert attributes["gen_ai.usage.output_tokens"] == 10
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 300
    assert attributes["gen_ai.usage.cache_creation.input_tokens"] == 50
    assert tuple(attributes["gen_ai.response.finish_reasons"]) == ("end_turn",)
    assert Decimal(str(attributes["trade_agent.cost_usd"])) > 0
    assert attributes["trade_agent.purpose"] == "triage"
    text_ = spans.attribute_text()
    assert "PROMPT-SECRETO" not in text_ and "NOTICIA-SIGILOSA" not in text_
    assert ("research", "llm") in spans.edges()


# ============================================================================ Telegram
API = "https://tg.example/botTOKEN123"


async def test_telegram_commands_and_alerts(spans: Recorded, store: Store) -> None:
    guard = RiskGuard(conditions=CONDITIONS, states=StateStore(store), store=store, notify=_ignore)
    with respx.mock() as router:
        send = router.post(f"{API}/sendMessage").mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": {}}),
                httpx.Response(500, json={"ok": False, "description": "falhou"}),
            ]
        )
        update = {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/pause global"}}
        router.post(f"{API}/getUpdates").respond(200, json={"ok": True, "result": [update]})
        async with httpx.AsyncClient() as http:
            bot = TelegramBot(http, "TOKEN123", base_url="https://tg.example")
            center = CommandCenter(
                bot=bot, chat_id=7, guard=guard, store=store, scopes=["conservador"], queries={}
            )
            await center.poll_once(timeout_s=0)
            await TelegramNotifier(bot, 7, min_severity=Severity.INFO).notify(
                Severity.HIGH, "alerta"
            )
    assert len(send.calls) == 2
    command = spans.one("telegram.command")
    assert spans.parent(command) is None  # each command is the root of a trace
    assert (command.attributes or {})["trade_agent.command"] == "/pause"
    assert (command.attributes or {})["trade_agent.authorized"] is True
    pause = spans.one("risk.pause")
    assert spans.parent(pause) == command
    assert (pause.attributes or {})["trade_agent.scope"] == "global"
    assert not spans.named("telegram getUpdates")  # the wait for messages is not traced
    reply, alert_call = spans.named("telegram sendMessage")
    assert spans.parent(reply) == command
    alert = spans.one("alert.send")
    assert spans.parent(alert_call) == alert and alert.status.status_code is StatusCode.ERROR
    assert (alert_call.attributes or {})["http.response.status_code"] == 500
    assert "TOKEN123" not in spans.attribute_text()
    assert {("telegram", "risk"), ("risk", "db")} <= spans.edges()


async def _ignore(_severity: Severity, _text: str) -> None:
    return None


# ============================================================================ runtime
async def test_background_task_failures_mark_their_trace(
    spans: Recorded, db: Database, api: BinanceSpotApi, store: Store
) -> None:
    runtime = AgentRuntime(
        db=db,
        api=api,
        store=store,
        reconciler=Reconciler(api, None, store),  # type: ignore[arg-type]
    )

    async def check_risk() -> None:
        raise RuntimeError("falha no risco")

    async def ping() -> None:
        await asyncio.sleep(0)

    await runtime._guarded(check_risk)
    await runtime._guarded(ping, trace=False)  # heartbeat: no trace
    job = spans.one("job check_risk")
    assert component(job) == "runtime" and job.status.status_code is StatusCode.ERROR
    assert job.events[0].attributes is not None
    assert "falha no risco" in str(job.events[0].attributes["exception.message"])
    assert not spans.named("job ping")
    assert json.dumps([s.name for s in spans.spans])  # no span without a name
