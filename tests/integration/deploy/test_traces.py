"""Traces: agente (OpenTelemetry, OTLP/HTTP) → Jaeger → Grafana, com as configurações de deploy/.

Um Jaeger com o ``config.yaml`` de produção recebe spans exportados pelo próprio
``trade_agent.tracing`` e deve mostrar cada componente como serviço, com o grafo de
dependências (System Architecture) montado a partir das relações pai → filho.
"""

import json
import re
import time
from collections.abc import Iterator

import pytest
import structlog
import yaml
from opentelemetry.trace import SpanKind

from tests.support.jaeger import JAEGER_CONFIG, Jaeger, jaeger_config, jaeger_container, port
from tests.support.logs import COMPOSE, DEPLOY, alloy_blocks, image
from tests.support.tracing import Recorded
from trade_agent import tracing
from trade_agent.config.settings import LogFormat
from trade_agent.log import configure_logging

DATASOURCES = DEPLOY / "grafana" / "provisioning" / "datasources"
EDGES = {
    ("runtime", "decision"),
    ("decision", "research"),
    ("research", "llm"),
    ("decision", "execution"),
    ("execution", "exchange"),
    ("execution", "db"),
}


def _datasource(name: str) -> dict[str, object]:
    (datasource,) = yaml.safe_load((DATASOURCES / name).read_text(encoding="utf-8"))["datasources"]
    return dict(datasource)


@pytest.fixture(scope="module")
def jaeger() -> Iterator[Jaeger]:
    yield from jaeger_container()


@pytest.fixture(scope="module")
def exported(jaeger: Jaeger) -> Jaeger:
    """Um ciclo de decisão completo, exportado de verdade por OTLP/HTTP."""
    traces = tracing.configure_tracing(jaeger.otlp, environment="test")
    assert traces is not None
    try:
        with (
            tracing.span("runtime", "job decide_conservador"),
            tracing.span("decision", "decision.cycle"),
        ):
            with (
                tracing.span("research", "research.cycle"),
                tracing.span("llm", "chat claude-opus-5", kind=SpanKind.CLIENT),
            ):
                pass
            with tracing.span("execution", "position.open"):
                with tracing.span("exchange", "POST /api/v3/orderList/opoco"):
                    pass
                with tracing.span("db", "INSERT intents"):
                    pass
    finally:
        traces.shutdown()  # envia a fila ao Jaeger
        tracing.install(None)
    services = {tracing.service_name(c) for edge in EDGES for c in edge}
    deadline = time.monotonic() + 30
    while not services <= set(jaeger.get("/api/services")["data"] or []):
        assert time.monotonic() < deadline, "spans não chegaram ao Jaeger"
        time.sleep(0.5)
    return jaeger


# ============================================================================ Jaeger real
def test_each_component_is_a_service_linked_by_calls(exported: Jaeger) -> None:
    now_ms = int(time.time() * 1000)
    links = exported.get("/api/dependencies", endTs=now_ms, lookback=3_600_000)["data"]
    prefix = f"{tracing.NAMESPACE}."
    edges = {
        (link["parent"].removeprefix(prefix), link["child"].removeprefix(prefix)) for link in links
    }
    assert edges == EDGES
    assert all(link["callCount"] == 1 for link in links)


def test_trace_keeps_the_whole_cycle(exported: Jaeger) -> None:
    (trace,) = exported.get("/api/traces", service="trade-agent.runtime", limit=5)["data"]
    assert len(trace["spans"]) == 7
    services = {p["serviceName"] for p in trace["processes"].values()}
    assert services == {tracing.service_name(c) for edge in EDGES for c in edge}
    operations = exported.get("/api/services/trade-agent.decision/operations")["data"]
    assert operations == ["decision.cycle"]


def test_grafana_api_v1_is_available(exported: Jaeger) -> None:
    """O datasource Jaeger do Grafana usa a API v1 (removida na Jaeger 2.21): trava a versão."""
    assert exported.get("/api/services")["data"]
    assert re.fullmatch(r"jaegertracing/jaeger:2\.20\.\d+", image("jaeger"))


# ============================================================================ configuração
def test_jaeger_keeps_7_days_on_the_volume_and_listens_on_localhost() -> None:
    config = jaeger_config()
    badger = config["extensions"]["jaeger_storage"]["backends"]["badger_store"]["badger"]
    assert badger["ttl"]["spans"] == "168h" and badger["ephemeral"] is False
    assert all(d.startswith("/tmp/jaeger/") for d in badger["directories"].values())  # noqa: S108
    # sem traces do próprio Jaeger (consulta e armazenamento) misturados aos do agente
    assert config["extensions"]["jaeger_query"]["enable_tracing"] is False
    assert config["service"]["telemetry"]["traces"]["level"] == "none"
    service = COMPOSE["services"]["jaeger"]
    assert "jaeger:/tmp" in service["volumes"]
    assert all(p.startswith("127.0.0.1:") for p in service["ports"])
    health = port(config["extensions"]["healthcheckv2"]["http"]["endpoint"])
    assert f"127.0.0.1:{health}/status" in " ".join(service["healthcheck"]["test"])
    assert f"{JAEGER_CONFIG.name}:/etc/jaeger/config.yaml:ro" in " ".join(service["volumes"])


def test_agent_exports_to_the_jaeger_otlp_receiver() -> None:
    otlp = jaeger_config()["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
    agent = COMPOSE["services"]["agent"]["environment"]
    assert agent["TA_OTLP_ENDPOINT"] == f"http://jaeger:{port(otlp)}"


def test_grafana_links_traces_and_logs_both_ways(
    spans: Recorded, capsys: pytest.CaptureFixture[str]
) -> None:
    jaeger = _datasource("jaeger.yaml")
    loki = _datasource("loki.yaml")
    assert (jaeger["uid"], jaeger["type"], jaeger["url"]) == (
        "trade-agent-jaeger",
        "jaeger",
        "http://jaeger:16686",
    )
    to_logs = jaeger["jsonData"]["tracesToLogsV2"]  # type: ignore[index]
    assert to_logs["datasourceUid"] == loki["uid"]
    assert to_logs["query"] == '{service="agent"} | trace_id="$${__trace.traceId}"'
    (derived,) = loki["jsonData"]["derivedFields"]  # type: ignore[index]
    assert derived["datasourceUid"] == jaeger["uid"] and derived["url"] == "$${__value.raw}"
    # a expressão do link encontra o trace_id num log real do agente
    configure_logging("INFO", LogFormat.JSON)
    with tracing.span("risk", "risk.snapshot") as current:
        structlog.get_logger("t").info("risk.snapshot")
    trace_id = format(current.get_span_context().trace_id, "032x")
    line = capsys.readouterr().out.strip()
    match = re.search(str(derived["matcherRegex"]), line)
    assert match and match.group(1) == trace_id and json.loads(line)["trace_id"] == trace_id
    assert spans.one("risk.snapshot").context.trace_id == current.get_span_context().trace_id
    # o Alloy guarda o trace_id do log como metadado (consulta do Grafana: | trace_id="...")
    assert "trace_id" in alloy_blocks()["loki.process"]
