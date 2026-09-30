"""Raiz de composição e comando ``trade-agent run`` (PostgreSQL + Binance simulada)."""

import asyncio
import io
from pathlib import Path

import httpx
import pytest
import yaml
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from tests.support.fake_binance import FakeBinance
from tests.support.metrics import Measured
from tests.support.tracing import Recorded
from trade_agent import app, cli, metrics, tracing
from trade_agent.app import build_runtime, install_signal_handlers, run_agent
from trade_agent.config.settings import Settings, load_settings
from trade_agent.exchange.errors import BinanceConfigurationError
from trade_agent.persistence.db import AlreadyRunningError, Database
from trade_agent.persistence.store import Store

PROFILES = Path(__file__).parents[1] / "fixtures" / "profiles.yaml"


def _settings(postgres_url: str | None, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "binance_api_key": "fake-key",
        "binance_key_type": "hmac",
        "binance_api_secret": "fake-secret",
        "database_url": postgres_url,
        "strategy_config": PROFILES,  # independente do config/ que o operador edita
    }
    values.update(overrides)
    return load_settings(env_file=None, **values)


async def test_build_runtime_requires_database_url() -> None:
    with pytest.raises(BinanceConfigurationError, match="TA_DATABASE_URL"):
        async with build_runtime(_settings(None)):
            pass


async def test_build_runtime_requires_credentials(postgres_url: str) -> None:
    settings = load_settings(env_file=None, database_url=postgres_url)
    with pytest.raises(BinanceConfigurationError, match="credenciais"):
        async with build_runtime(settings):
            pass


async def test_build_runtime_wires_user_stream(postgres_url: str) -> None:
    async with build_runtime(_settings(postgres_url)) as runtime:
        assert runtime._events is not None
    async with build_runtime(_settings(postgres_url), user_stream=False) as runtime:
        assert runtime._events is None


async def test_build_runtime_with_llm_key_and_injected_http(postgres_url: str) -> None:
    settings = _settings(postgres_url, anthropic_api_key="sk-test")
    async with (
        httpx.AsyncClient() as aux,
        build_runtime(settings, aux_http=aux, user_stream=False) as runtime,
    ):
        assert [tf for tf, _ in runtime._candle_jobs] == ["4h", "1h"]  # agressivo desligado
        assert len(runtime._periodic) == 2  # risco (com telemetria) e notícias
        assert runtime._heartbeat is None  # sem TA_HEALTHCHECK_URL


async def test_run_agent_starts_recovers_and_stops(postgres_url: str, db: Database) -> None:
    fake = FakeBinance()
    fake.candles[("BTCUSDT", "1m")] = [[0, "0", "0", "0", "100", "1", 0, "1", 1, "0", "0", "0"]]
    offline: list[str] = []

    def no_internet(request: httpx.Request) -> httpx.Response:
        offline.append(request.url.host)  # a coleta de notícias roda já na partida
        return httpx.Response(503)

    stop = asyncio.Event()
    async with (
        httpx.AsyncClient(base_url="https://fake.binance", transport=fake.transport) as http,
        httpx.AsyncClient(transport=httpx.MockTransport(no_internet)) as aux,
    ):
        task = asyncio.create_task(
            run_agent(
                _settings(postgres_url),
                stop=stop,
                http_client=http,
                aux_http=aux,
                user_stream=False,
            )
        )
        store = Store(db)
        for _ in range(200):
            recorded = await Store(db).get_checkpoint("risk.equity")
            if recorded is not None and offline:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    kinds = [e.kind for e in await store.recent_events()]
    assert kinds[0] == "agent.stopped" and "agent.started" in kinds
    assert "runtime.task_failed" not in kinds  # risco e coleta rodaram já na partida, sem falha
    assert fake.calls("GET", "/api/v3/time")
    assert "www.coindesk.com" in offline  # nenhum acesso real à internet nos testes


async def test_run_agent_traces_the_components(
    postgres_url: str, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = InMemorySpanExporter()
    configured: list[tuple[str | None, str]] = []

    def in_memory(endpoint: str | None, *, environment: str) -> tracing.Tracing:
        configured.append((endpoint, environment))
        traces = tracing.Tracing(SimpleSpanProcessor(exporter), environment=environment)
        tracing.install(traces)
        return traces

    reader = InMemoryMetricReader()

    def in_memory_metrics(endpoint: str | None, *, environment: str) -> metrics.AgentMetrics:
        configured.append((endpoint, environment))
        agent_metrics = metrics.AgentMetrics(reader, environment=environment)
        metrics.install(agent_metrics)
        return agent_metrics

    monkeypatch.setattr(app, "configure_tracing", in_memory)
    monkeypatch.setattr(app, "configure_metrics", in_memory_metrics)
    fake = FakeBinance()
    fake.candles[("BTCUSDT", "1m")] = [[0, "0", "0", "0", "100", "1", 0, "1", 1, "0", "0", "0"]]
    stop = asyncio.Event()
    settings = _settings(
        postgres_url,
        otlp_endpoint="http://jaeger:4318,http://signoz-ingester:4318",
        otlp_metrics_endpoint="http://signoz-ingester:4318",
    )
    async with (
        httpx.AsyncClient(base_url="https://fake.binance", transport=fake.transport) as http,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as aux,
    ):
        task = asyncio.create_task(
            run_agent(settings, stop=stop, http_client=http, aux_http=aux, user_stream=False)
        )
        recorded, measured = Recorded(exporter), Measured(reader)
        for _ in range(200):
            names = {s.name for s in recorded.spans}
            if {"job check_risk", "job ingest"} <= names and "trade_agent.equity" in (
                points := measured.points()
            ):
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert configured == [
        ("http://jaeger:4318,http://signoz-ingester:4318", "testnet"),
        ("http://signoz-ingester:4318", "testnet"),
    ]
    assert tracing._active is None and not metrics.enabled()  # desinstalados ao encerrar
    assert points["trade_agent.equity"] == [({}, 1000.0)]  # a verificação de risco alimentou
    names = {s.name for s in recorded.spans}
    assert {"agent.start", "reconcile.all", "risk.snapshot", "risk.apply"} <= names
    assert {"telemetry.observe", "research.ingest", "research.collect"} <= names
    assert {
        ("runtime", "reconcile"),
        ("reconcile", "exchange"),
        ("runtime", "risk"),
        ("risk", "exchange"),
        ("risk", "db"),
        ("runtime", "telemetry"),
        ("telemetry", "db"),
        ("runtime", "research"),
        ("runtime", "db"),  # migrações e eventos da partida
    } <= recorded.edges()
    offline = [s for s in recorded.spans if s.name.startswith("source ")]
    assert offline and all(s.status.status_code is StatusCode.ERROR for s in offline)
    text_ = recorded.attribute_text()
    assert "fake-secret" not in text_ and "fake-key" not in text_ and "signature" not in text_


async def test_install_signal_handlers_is_safe() -> None:
    install_signal_handlers(asyncio.Event())  # no Windows, o SO não suporta: ignorado


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception | None
) -> tuple[int, str]:
    calls: list[Settings] = []

    async def fake_run_agent(settings: Settings) -> None:
        calls.append(settings)
        if error is not None:
            raise error

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    env_file = tmp_path / ".env"
    env_file.write_text("TA_LOG_LEVEL=WARNING\n", encoding="utf-8")
    err = io.StringIO()
    code = cli.main(["--env-file", str(env_file), "run"], out=io.StringIO(), err=err)
    assert len(calls) == 1
    return code, err.getvalue()


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (None, 0, ""),
        (AlreadyRunningError("outra instância"), 1, "outra instância"),
        (BinanceConfigurationError("sem banco"), 1, "sem banco"),
        (FileNotFoundError("config/profiles.yaml"), 1, "Configuração ausente ou inválida"),
        (yaml.YAMLError("tabulação inválida"), 1, "tabulação inválida"),
    ],
)
def test_cli_run_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception | None,
    code: int,
    message: str,
) -> None:
    result, err = _run_cli(monkeypatch, tmp_path, error)
    assert result == code
    assert message in err
