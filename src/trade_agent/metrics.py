"""Agent metrics with OpenTelemetry, exported over OTLP/HTTP (to SigNoz).

On every risk check (1 min), the latest reading feeds the gauges (equity, drawdown, PnL,
exposure, positions, API errors, used weight, clock, and the result and the state of each
scope); every decision cycle feeds the market regime's gauge (the benchmark against its
slow EMA, per timeframe).
Counters record what happens between readings: LLM cost and tokens, decision cycles,
setups, setups removed by the stop-noise filter, entries, exits and risk state changes.

Latency, throughput and errors per operation are not here: SigNoz derives them from the
traces. Without ``TA_OTLP_METRICS_ENDPOINT``, nothing is installed and the calls do nothing.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from importlib.metadata import version

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from trade_agent.tracing import NAMESPACE

SERVICE = NAMESPACE
EXPORT_INTERVAL_MS = 60_000
STATE_CODES = {"running": 0, "paused": 1, "halted": 2, "flattening": 3}
"""Risk state as a number (0 = running): the chart goes up when the agent stops."""


@dataclass(frozen=True, slots=True)
class Reading:
    """The agent's latest reading (from the risk check)."""

    equity: Decimal
    day_start_equity: Decimal
    peak_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    exposure: Decimal
    active_positions: int
    api_error_rate: float
    clock_offset_ms: int
    used_weight_1m: int | None = None
    states: Mapping[str, str] = field(default_factory=dict)
    scope_pnl: Mapping[str, Decimal] = field(default_factory=dict)
    """Result since the start (realized + open) per scope: ``global`` and each profile."""

    @property
    def drawdown_pct(self) -> float:
        peak = self.peak_equity
        return float((peak - self.equity) / peak * 100) if peak > 0 else 0.0


class AgentMetrics:
    def __init__(self, reader: MetricReader, *, environment: str) -> None:
        resource = Resource.create(
            {
                "service.name": SERVICE,
                "service.namespace": NAMESPACE,
                "service.version": version("trade-agent"),
                "deployment.environment": environment,
                "deployment.environment.name": environment,
            }
        )
        self._provider = MeterProvider(
            resource=resource, metric_readers=[reader], shutdown_on_exit=False
        )
        self._latest: Reading | None = None
        self._regimes: dict[str, float] = {}
        meter = self._provider.get_meter("trade_agent")
        gauges: dict[str, tuple[str, str]] = {
            "trade_agent.equity": ("USD", "patrimônio do agente (capital + realizado + aberto)"),
            "trade_agent.equity.day_start": ("USD", "patrimônio na abertura do dia (UTC)"),
            "trade_agent.equity.peak": ("USD", "pico do patrimônio"),
            "trade_agent.drawdown": ("%", "queda desde o pico"),
            "trade_agent.pnl.daily": ("USD", "patrimônio menos a abertura do dia"),
            "trade_agent.pnl.realized": ("USD", "PnL realizado acumulado"),
            "trade_agent.pnl.unrealized": ("USD", "PnL das posições abertas (a preço de venda)"),
            "trade_agent.exposure": ("USD", "custo das posições ativas"),
            "trade_agent.positions.active": ("{position}", "posições ativas"),
            "trade_agent.binance.error_rate": ("1", "falhas de infraestrutura em 5 min"),
            "trade_agent.binance.weight_used_1m": ("{weight}", "peso usado no minuto"),
            "trade_agent.clock.offset": ("ms", "relógio da Binance menos o local"),
        }
        for name, (unit, description) in gauges.items():
            meter.create_observable_gauge(
                name, callbacks=[self._gauge(name)], unit=unit, description=description
            )
        meter.create_observable_gauge(
            "trade_agent.pnl.total",
            callbacks=[self._scope_pnl],
            unit="USD",
            description="resultado acumulado por escopo (realizado + aberto)",
        )
        meter.create_observable_gauge(
            "trade_agent.market.regime",
            callbacks=[self._regime],
            unit="%",
            description="fechamento do BTC acima (+) ou abaixo (−) da EMA200, por timeframe",
        )
        meter.create_observable_gauge(
            "trade_agent.risk.state",
            callbacks=[self._states],
            unit="1",
            description="estado por escopo: 0 operando, 1 pausado, 2 parado, 3 vendendo tudo",
        )
        self.llm_cost = meter.create_counter(
            "trade_agent.llm.cost", unit="USD", description="custo das chamadas ao LLM"
        )
        self.llm_tokens = meter.create_counter(
            "trade_agent.llm.tokens", unit="{token}", description="tokens enviados e recebidos"
        )
        self.cycles = meter.create_counter(
            "trade_agent.decision.cycles", unit="{cycle}", description="ciclos de decisão"
        )
        self.setups = meter.create_counter(
            "trade_agent.decision.setups", unit="{setup}", description="setups encontrados"
        )
        self.stop_noise = meter.create_counter(
            "trade_agent.decision.stop_noise",
            unit="{setup}",
            description="setups removidos pelo filtro de stop no ruído (D-039)",
        )
        self.entries = meter.create_counter(
            "trade_agent.decision.entries", unit="{position}", description="entradas abertas"
        )
        self.exits = meter.create_counter(
            "trade_agent.decision.exits", unit="{position}", description="saídas decididas"
        )
        self.state_changes = meter.create_counter(
            "trade_agent.risk.state_changes", unit="{change}", description="mudanças de estado"
        )

    def _value(self, name: str, reading: Reading) -> float | None:
        values: dict[str, Decimal | float | int | None] = {
            "trade_agent.equity": reading.equity,
            "trade_agent.equity.day_start": reading.day_start_equity,
            "trade_agent.equity.peak": reading.peak_equity,
            "trade_agent.drawdown": reading.drawdown_pct,
            "trade_agent.pnl.daily": reading.equity - reading.day_start_equity,
            "trade_agent.pnl.realized": reading.realized_pnl,
            "trade_agent.pnl.unrealized": reading.unrealized_pnl,
            "trade_agent.exposure": reading.exposure,
            "trade_agent.positions.active": reading.active_positions,
            "trade_agent.binance.error_rate": reading.api_error_rate,
            "trade_agent.binance.weight_used_1m": reading.used_weight_1m,
            "trade_agent.clock.offset": reading.clock_offset_ms,
        }
        value = values[name]
        return None if value is None else float(value)

    def _gauge(self, name: str) -> Callable[[CallbackOptions], Iterable[Observation]]:
        def callback(_options: CallbackOptions) -> Iterable[Observation]:
            reading = self._latest
            value = None if reading is None else self._value(name, reading)
            return [] if value is None else [Observation(value)]

        return callback

    def _states(self, _options: CallbackOptions) -> Iterable[Observation]:
        reading = self._latest
        if reading is None:
            return []
        return [
            Observation(STATE_CODES.get(state, -1), {"scope": scope})
            for scope, state in reading.states.items()
        ]

    def _scope_pnl(self, _options: CallbackOptions) -> Iterable[Observation]:
        reading = self._latest
        if reading is None:
            return []
        return [
            Observation(float(value), {"scope": scope})
            for scope, value in reading.scope_pnl.items()
        ]

    def _regime(self, _options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(v, {"timeframe": tf}) for tf, v in self._regimes.items()]

    def observe(self, reading: Reading) -> None:
        self._latest = reading

    def observe_regime(self, timeframe: str, distance_pct: float) -> None:
        self._regimes[timeframe] = distance_pct

    def shutdown(self) -> None:
        self._provider.shutdown()


_active: AgentMetrics | None = None


def install(metrics: AgentMetrics | None) -> None:
    global _active  # noqa: PLW0603 - a single instance per process
    _active = metrics


def enabled() -> bool:
    return _active is not None


def configure_metrics(endpoint: str | None, *, environment: str) -> AgentMetrics | None:
    """Turns on the OTLP/HTTP export of metrics to ``endpoint`` (every minute)."""
    if endpoint is None:
        install(None)
        return None
    exporter = OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics", timeout=5)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=EXPORT_INTERVAL_MS)
    metrics = AgentMetrics(reader, environment=environment)
    install(metrics)
    return metrics


def observe(reading: Reading) -> None:
    if _active is not None:
        _active.observe(reading)


def prime(*, llm: Iterable[tuple[str, str]], profiles: Iterable[str], dry_run: bool) -> None:
    """Exports the known counter series at 0 before their first event.

    A cumulative counter only has a series after its first ``add``, and that first point
    already carries the increment: with no earlier point, ``increase``/``rate`` (SigNoz)
    see nothing, so the first LLM call or cycle after every restart vanished from the
    charts. ``llm`` holds the ``(model, purpose)`` pairs the analyst uses.
    """
    if _active is None:
        return
    for model, purpose in llm:
        attributes = {"model": model, "purpose": purpose}
        _active.llm_cost.add(0, attributes)
        _active.llm_tokens.add(0, {**attributes, "direction": "input"})
        _active.llm_tokens.add(0, {**attributes, "direction": "output"})
    for profile in profiles:
        _active.cycles.add(0, {"profile": profile, "state": "running", "dry_run": dry_run})
        _active.setups.add(0, {"profile": profile})
        _active.stop_noise.add(0, {"profile": profile})
        _active.entries.add(0, {"profile": profile, "dry_run": dry_run})
        _active.exits.add(0, {"profile": profile, "dry_run": dry_run})


def record_llm(
    *, model: str, purpose: str, cost_usd: Decimal, input_tokens: int, output_tokens: int
) -> None:
    if _active is None:
        return
    attributes = {"model": model, "purpose": purpose}
    _active.llm_cost.add(float(cost_usd), attributes)
    _active.llm_tokens.add(input_tokens, {**attributes, "direction": "input"})
    _active.llm_tokens.add(output_tokens, {**attributes, "direction": "output"})


def record_cycle(
    *,
    profile: str,
    state: str,
    dry_run: bool,
    opened: int,
    exits: int,
    setups: int = 0,
    stop_noise: int = 0,
) -> None:
    if _active is None:
        return
    _active.cycles.add(1, {"profile": profile, "state": state, "dry_run": dry_run})
    if setups:
        _active.setups.add(setups, {"profile": profile})
    if stop_noise:
        _active.stop_noise.add(stop_noise, {"profile": profile})
    if opened:
        _active.entries.add(opened, {"profile": profile, "dry_run": dry_run})
    if exits:
        _active.exits.add(exits, {"profile": profile, "dry_run": dry_run})


def observe_regime(*, timeframe: str, distance_pct: float) -> None:
    """The benchmark's distance to its slow EMA on the cycle's candle (> 0: regime up)."""
    if _active is not None:
        _active.observe_regime(timeframe, distance_pct)


def record_state_change(*, scope: str, state: str, source: str) -> None:
    if _active is not None:
        _active.state_changes.add(1, {"scope": scope, "state": state, "source": source})
