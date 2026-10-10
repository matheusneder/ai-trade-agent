"""Agent metrics (OpenTelemetry): gauges of the latest reading and event counters."""

from collections.abc import Sequence
from decimal import Decimal

import pytest
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)

from tests.support.metrics import Measured
from trade_agent import metrics

D = Decimal
READING = metrics.Reading(
    equity=D(950),
    day_start_equity=D(1000),
    peak_equity=D(1000),
    realized_pnl=D(-40),
    unrealized_pnl=D(-10),
    exposure=D(300),
    active_positions=2,
    api_error_rate=0.25,
    clock_offset_ms=-120,
    used_weight_1m=42,
    states={"global": "running", "conservador": "paused", "moderado": "desconhecido"},
    scope_pnl={"global": D(-50), "conservador": D(-55), "moderado": D(5)},
)


def test_without_configuration_nothing_is_recorded() -> None:
    assert metrics.configure_metrics(None, environment="demo") is None
    assert not metrics.enabled()
    metrics.observe(READING)
    metrics.record_llm(model="m", purpose="p", cost_usd=D(1), input_tokens=1, output_tokens=1)
    metrics.record_cycle(profile="c", state="running", dry_run=True, opened=1, exits=1)
    metrics.record_state_change(scope="global", state="halted", source="gatilho")


def test_gauges_follow_the_latest_reading(measured: Measured) -> None:
    assert metrics.enabled()
    points = measured.points()
    assert "trade_agent.equity" not in points  # no reading: nothing to measure
    assert "trade_agent.pnl.total" not in points
    metrics.observe(READING)
    assert measured.value("trade_agent.equity") == 950
    assert measured.value("trade_agent.equity.day_start") == 1000
    assert measured.value("trade_agent.drawdown") == pytest.approx(5.0)
    assert measured.value("trade_agent.pnl.daily") == -50
    assert measured.value("trade_agent.pnl.realized") == -40
    assert measured.value("trade_agent.pnl.unrealized") == -10
    assert measured.value("trade_agent.exposure") == 300
    assert measured.value("trade_agent.positions.active") == 2
    assert measured.value("trade_agent.binance.error_rate") == 0.25
    assert measured.value("trade_agent.binance.weight_used_1m") == 42
    assert measured.value("trade_agent.clock.offset") == -120
    assert measured.value("trade_agent.risk.state", scope="global") == 0
    assert measured.value("trade_agent.risk.state", scope="conservador") == 1
    assert measured.value("trade_agent.risk.state", scope="moderado") == -1
    assert measured.value("trade_agent.pnl.total", scope="global") == -50
    assert measured.value("trade_agent.pnl.total", scope="conservador") == -55
    assert measured.value("trade_agent.pnl.total", scope="moderado") == 5
    resource = measured.resource()
    assert (resource["service.name"], resource["deployment.environment"]) == ("trade-agent", "test")


def test_gauges_skip_unknown_values_and_zero_peak(measured: Measured) -> None:
    metrics.observe(
        metrics.Reading(
            equity=D(0), day_start_equity=D(0), peak_equity=D(0), realized_pnl=D(0),
            unrealized_pnl=D(0), exposure=D(0), active_positions=0, api_error_rate=0.0,
            clock_offset_ms=0,
        )
    )  # fmt: skip
    points = measured.points()
    assert "trade_agent.binance.weight_used_1m" not in points  # weight still unknown
    assert measured.value("trade_agent.drawdown") == 0
    assert "trade_agent.risk.state" not in points
    assert "trade_agent.pnl.total" not in points  # no scope in the reading


def test_counters(measured: Measured) -> None:
    metrics.record_llm(
        model="claude-sonnet-5", purpose="triage", cost_usd=D("0.25"), input_tokens=100,
        output_tokens=10,
    )  # fmt: skip
    metrics.record_cycle(profile="conservador", state="running", dry_run=False, opened=1, exits=2)
    metrics.record_cycle(profile="conservador", state="paused", dry_run=False, opened=0, exits=0)
    metrics.record_state_change(scope="global", state="halted", source="gatilho")
    llm = {"model": "claude-sonnet-5", "purpose": "triage"}
    assert measured.value("trade_agent.llm.cost", **llm) == 0.25
    assert measured.value("trade_agent.llm.tokens", **llm, direction="input") == 100
    assert measured.value("trade_agent.llm.tokens", **llm, direction="output") == 10
    running = {"profile": "conservador", "state": "running", "dry_run": False}
    assert measured.value("trade_agent.decision.cycles", **running) == 1
    assert measured.value("trade_agent.decision.entries", profile="conservador", dry_run=False) == 1
    assert measured.value("trade_agent.decision.exits", profile="conservador", dry_run=False) == 2
    assert (
        measured.value(
            "trade_agent.risk.state_changes", scope="global", state="halted", source="gatilho"
        )
        == 1
    )


class FakeExporter(MetricExporter):
    instances: list["FakeExporter"] = []  # noqa: RUF012 - the test's record

    def __init__(self, *, endpoint: str, timeout: float) -> None:
        super().__init__()
        self.endpoint, self.timeout = endpoint, timeout
        self.exported: list[MetricsData] = []
        FakeExporter.instances.append(self)

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object
    ) -> MetricExportResult:
        self.exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        return None


def test_configure_metrics_exports_to_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "OTLPMetricExporter", FakeExporter)
    FakeExporter.instances.clear()
    agent_metrics = metrics.configure_metrics("http://signoz-ingester:4318/", environment="demo")
    assert agent_metrics is not None and metrics.enabled()
    metrics.observe(READING)
    agent_metrics.shutdown()  # last export
    metrics.install(None)
    (exporter,) = FakeExporter.instances
    assert exporter.endpoint == "http://signoz-ingester:4318/v1/metrics"
    names = _names(exporter.exported)
    assert {"trade_agent.equity", "trade_agent.risk.state"} <= names


def _names(batches: Sequence[MetricsData]) -> set[str]:
    return {
        metric.name
        for data in batches
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
