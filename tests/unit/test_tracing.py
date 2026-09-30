"""Rastreamento (OpenTelemetry): um serviço por componente, processador compartilhado."""

import json
from collections.abc import Sequence
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import NoOpTracer, StatusCode

from tests.support.tracing import Recorded, component
from trade_agent import tracing
from trade_agent.config.settings import LogFormat
from trade_agent.log import configure_logging
from trade_agent.persistence.db import _end_span, _fail_span, _start_span, statement_span_name
from trade_agent.runtime import job_name


class Color(Enum):
    RED = "vermelho"


class FakeExporter(SpanExporter):
    instances: list["FakeExporter"] = []  # noqa: RUF012 - registro do teste

    def __init__(self, *, endpoint: str, timeout: float) -> None:
        self.endpoint, self.timeout = endpoint, timeout
        self.exported: list[ReadableSpan] = []
        self.closed = False
        FakeExporter.instances.append(self)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.closed = True


def test_without_configuration_spans_are_free() -> None:
    assert isinstance(tracing.tracer("risk"), NoOpTracer)
    with tracing.span("risk", "risk.snapshot") as current:
        assert not current.is_recording()
        tracing.annotate(equity=Decimal(1))  # sem efeito, sem erro
        assert tracing.add_trace_ids(None, "info", {}) == {}
    assert tracing.configure_tracing(None, environment="demo") is None


def test_configure_tracing_exports_every_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "OTLPSpanExporter", FakeExporter)
    FakeExporter.instances.clear()
    traces = tracing.configure_tracing("http://jaeger:4318/", environment="demo")
    assert traces is not None
    with tracing.span("runtime", "job check_risk"), tracing.span("risk", "risk.snapshot"):
        pass
    traces.shutdown()  # uma vez: envia a fila e fecha o exportador compartilhado
    tracing.install(None)
    (exporter,) = FakeExporter.instances
    assert (exporter.endpoint, exporter.timeout, exporter.closed) == (
        "http://jaeger:4318/v1/traces",
        5,
        True,
    )
    risk, runtime = exporter.exported
    assert (risk.name, runtime.name) == ("risk.snapshot", "job check_risk")
    assert risk.resource.attributes["service.name"] == "trade-agent.risk"
    assert runtime.resource.attributes["service.name"] == "trade-agent.runtime"
    assert risk.resource.attributes["service.namespace"] == "trade-agent"
    assert risk.resource.attributes["deployment.environment.name"] == "demo"
    assert risk.parent is not None and risk.parent.span_id == runtime.context.span_id


def test_components_link_like_jaeger(spans: Recorded) -> None:
    with tracing.span("runtime", "job decide"), tracing.span("decision", "decision.cycle"):
        with tracing.span("exchange", "GET /api/v3/klines"):
            pass
        with tracing.span("decision", "universe.get"):
            pass
    assert spans.edges() == {("runtime", "decision"), ("decision", "exchange")}
    assert {component(s) for s in spans.spans} == {"runtime", "decision", "exchange"}
    assert len({s.context.trace_id for s in spans.spans}) == 1
    assert set(tracing.COMPONENTS) >= {"runtime", "db", "llm", "telegram"}
    assert tracing.service_name("db") == "trade-agent.db"


def test_annotate_converts_values(spans: Recorded) -> None:
    with tracing.span("risk", "x"):
        tracing.annotate(
            equity=Decimal("1000.5"),
            state=Color.RED,
            hits=["a", "b"],
            positions=2,
            ratio=0.5,
            ok=True,
            name="texto",
            missing=None,
            other=SimpleNamespace(v=1),
        )
    attributes = spans.one("x").attributes or {}
    assert attributes["trade_agent.equity"] == "1000.5"
    assert attributes["trade_agent.state"] == "vermelho"
    assert tuple(attributes["trade_agent.hits"]) == ("a", "b")
    assert (attributes["trade_agent.positions"], attributes["trade_agent.ratio"]) == (2, 0.5)
    assert attributes["trade_agent.ok"] is True and attributes["trade_agent.name"] == "texto"
    assert "trade_agent.missing" not in attributes
    assert attributes["trade_agent.other"] == "namespace(v=1)"


async def test_traced_decorator_records_success_and_failure(spans: Recorded) -> None:
    @tracing.traced("execution", "position.open")
    async def open_position(symbol: str) -> str:
        tracing.annotate(symbol=symbol)
        if symbol == "FAIL":
            raise RuntimeError("rejeitada")
        return f"ok {symbol}"

    assert open_position.__name__ == "open_position"
    assert await open_position("BTCUSDT") == "ok BTCUSDT"
    with pytest.raises(RuntimeError):
        await open_position("FAIL")
    ok, failed = spans.named("position.open")
    assert ok.status.status_code is StatusCode.UNSET
    assert (ok.attributes or {})["trade_agent.symbol"] == "BTCUSDT"
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.events[0].name == "exception"


def test_fail_marks_a_handled_error(spans: Recorded) -> None:
    with tracing.span("telegram", "alert.send") as current:
        tracing.fail(current, ValueError("sem rede"))
    failed = spans.one("alert.send")
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.status.description == "ValueError"


def test_logs_carry_the_trace_ids(spans: Recorded, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.JSON)
    with tracing.span("risk", "risk.apply") as current:
        structlog.get_logger("t").info("risk.evaluated")
    structlog.get_logger("t").info("fora")
    inside, outside = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    context = current.get_span_context()
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")
    assert "trace_id" not in outside


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("SELECT positions.id FROM positions WHERE state = $1", ("SELECT positions", "positions")),
        ("INSERT INTO events (kind) VALUES ($1)", ("INSERT events", "events")),
        ("UPDATE checkpoints SET value = $1", ("UPDATE checkpoints", "checkpoints")),
        ("TRUNCATE positions, intents RESTART IDENTITY", ("TRUNCATE positions", "positions")),
        ("SELECT pg_try_advisory_lock($1)", ("SELECT", None)),
        ("   ", ("SQL", None)),
    ],
)
def test_statement_span_name(statement: str, expected: tuple[str, str | None]) -> None:
    assert statement_span_name(statement) == expected


def test_sql_listeners_ignore_statements_without_span() -> None:
    no_span = SimpleNamespace()
    _end_span(None, None, "SELECT 1", None, no_span, False)  # nada a encerrar
    _fail_span(SimpleNamespace(execution_context=None, original_exception=OSError()))  # type: ignore[arg-type]


def test_sql_failure_without_sqlstate(spans: Recorded) -> None:
    context = SimpleNamespace()
    conn = SimpleNamespace(engine=SimpleNamespace(url=SimpleNamespace(host=None)))
    _start_span(conn, None, "SELECT 1", None, context, False)
    _fail_span(SimpleNamespace(execution_context=context, original_exception=OSError("x")))  # type: ignore[arg-type]
    failed = spans.one("SELECT")
    assert (failed.attributes or {})["error.type"] == "OSError"
    assert "db.response.status_code" not in (failed.attributes or {})
    assert (failed.attributes or {})["server.address"] == ""


def _job() -> None: ...


@pytest.mark.parametrize(
    ("qualname", "expected"),
    [
        ("assemble.<locals>.check_risk", "check_risk"),
        ("AgentRuntime._reconcile", "reconcile"),
        ("decide_conservador", "decide_conservador"),
    ],
)
def test_job_name(qualname: str, expected: str) -> None:
    action: Any = _job
    action.__qualname__ = qualname
    assert job_name(action) == expected
    assert job_name(SimpleNamespace()) == "SimpleNamespace"  # type: ignore[arg-type]
