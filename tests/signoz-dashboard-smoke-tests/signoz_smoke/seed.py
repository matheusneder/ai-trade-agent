"""Synthetic, deterministic telemetry of a normal day of the agent, for the SigNoz dashboards.

Everything here is pure: the same ``seed``, ``hours`` and ``end`` always give the same spans,
logs and metrics (``random.Random(seed)``, no clock reads). :mod:`signoz_smoke.otlp` turns
the result into OTLP requests and sends them to an ingester.

The data imitates what the real stack emits, with the same names and attributes:

* traces of the agent's components (``trade-agent.<component>``, see ``trade_agent.tracing``):
  the 1-minute risk check, reconciliation, clock sync, news ingestion and the 4-hour decision
  cycles of each profile, with Binance (``trade-agent.exchange``), PostgreSQL
  (``trade-agent.db``) and Claude (``trade-agent.llm``) calls below them;
* the agent's metrics (``trade_agent.*``, see ``trade_agent.metrics``): gauges every minute
  from one portfolio simulation, and cumulative counters (LLM cost and tokens, cycles,
  entries, exits, risk state changes) fed by the same events that create the spans;
* the logs that Alloy forwards (``service.name`` = compose service, structlog JSON flattened
  into attributes, ``trace_id``/``span_id`` of the span that wrote them);
* the ``docker_stats`` metrics of each compose service (``deploy/otelcol``).

Activity follows the day: Binance traffic, latency, request weight, CPU and log volume peak
in the afternoon (UTC) and bottom out at night. Rare events (an HTTP 429/5xx, a failed task,
a risk pause, an entry and a rule exit) are guaranteed at least once, so every panel of every
dashboard has data. :class:`Summary` holds the totals the dashboards must show.
"""

import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum

type AttributeValue = str | bool | int | float
type Attributes = Mapping[str, AttributeValue]

SECOND = 1_000_000_000
MINUTE = 60 * SECOND
MS = 1_000_000
ENVIRONMENT = "smoke"
VERSION = "0.1.0"
NAMESPACE = "trade-agent"
PROFILES = ("swing_trend", "momentum_alpha")
SCOPES = ("global", *PROFILES)
STATE_CODES = {"running": 0, "paused": 1, "halted": 2, "flattening": 3}
CAPITAL = 1000.0
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT", "WLDUSDT", "LINKUSDT", "AVAXUSDT")
NEWS_SOURCES = (
    "binance",
    "derivatives",
    "fear_greed",
    "rss:coindesk",
    "rss:cointelegraph",
    "rss:decrypt",
    "rss:theblock",
)
# (model, purpose, input tokens, output tokens, seconds): the analyst's three calls per cycle
LLM_CALLS = (
    ("claude-sonnet-5", "triage", 3_000, 1_700, 10.5),
    ("claude-opus-5", "research", 250_000, 8_000, 45.0),
    ("claude-opus-5", "analyst", 6_500, 950, 28.0),
)
LLM_PRICES = {"claude-sonnet-5": (3.0, 15.0), "claude-opus-5": (5.0, 25.0)}  # US$ / Mtok
HTTP_FAILURES = {429: 4, 503: 3, 418: 1, 400: 2, 500: 1}  # status: weight
# compose service: (CPU %, memory MiB, network KiB/min, disk KiB/min)
CONTAINERS = {
    "agent": (2.5, 210, 900, 40),
    "postgres": (1.2, 160, 300, 220),
    "grafana": (0.8, 120, 60, 10),
    "loki": (1.0, 180, 200, 300),
    "alloy": (0.9, 140, 260, 30),
    "jaeger": (0.7, 150, 220, 60),
    "jaeger-ui": (0.1, 12, 5, 0),
    "docker-proxy": (0.2, 20, 120, 0),
    "container-metrics": (0.6, 90, 140, 5),
    "ingester": (1.8, 260, 700, 20),
    "signoz-signoz-0": (1.5, 420, 400, 30),
    "signoz-telemetrystore-clickhouse-0-0": (6.0, 1_400, 900, 1_500),
    "signoz-telemetrykeeper-clickhousekeeper-0": (0.5, 90, 50, 80),
    "signoz-metastore-postgres-0": (0.3, 60, 30, 40),
}
MEMORY_LIMIT = 8 * 1024**3  # no limit set: Docker reports the VM's memory


class SpanKind(IntEnum):
    """OTLP span kinds (the numbers of the protocol)."""

    INTERNAL = 1
    CLIENT = 3


@dataclass(frozen=True, slots=True)
class Span:
    trace_id: bytes
    span_id: bytes
    parent_id: bytes  # b"" for a root span
    service: str
    name: str
    kind: SpanKind
    start: int  # unix ns
    end: int
    attributes: Attributes
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LogRecord:
    time: int
    service: str  # compose service, the resource's service.name
    severity: str
    body: str
    attributes: Attributes
    trace_id: bytes = b""
    span_id: bytes = b""


@dataclass(frozen=True, slots=True)
class Point:
    time: int
    value: float
    attributes: Mapping[str, str | bool] = field(default_factory=dict)


@dataclass(slots=True)
class Metric:
    name: str
    unit: str
    kind: str  # "gauge" or "sum" (cumulative)
    resource: tuple[tuple[str, str], ...]
    points: list[Point] = field(default_factory=list)
    monotonic: bool = True
    start: int = 0  # start of the cumulative sums


@dataclass(frozen=True, slots=True)
class Summary:
    """What the dashboards must show for the seeded window."""

    seed: int
    start: int
    end: int
    spans: int
    logs: int
    metric_points: int
    exchange_requests: int
    exchange_http_errors: Mapping[int, int]
    job_failures: int
    error_logs: int
    llm_calls: int
    llm_cost: float
    llm_tokens: int
    decision_cycles: Mapping[str, int]
    entries: Mapping[str, int]  # per profile
    exits: Mapping[str, int]
    state_changes: list[str]  # "scope → state" series
    containers: list[str]  # compose services
    services: list[str]  # span services
    jobs: list[str]  # job span names
    failed_jobs: list[str]
    llm_series: list[str]  # "model · purpose"
    log_services: list[str]
    problem_logs: list[str]  # "service severity" of the warnings and errors
    agent_problems: int
    last: Mapping[str, float]  # last value of each gauge of the "Agora" row

    def to_json(self) -> str:
        return json.dumps(
            {k: getattr(self, k) for k in self.__dataclass_fields__},
            indent=2,
            default=dict,
        )


@dataclass(slots=True)
class Telemetry:
    spans: list[Span]
    logs: list[LogRecord]
    metrics: list[Metric]
    summary: Summary


def activity(at_ns: int) -> float:
    """0.55 at 03:00 UTC up to 1.0 at 15:00 UTC: markets and the agent's traffic follow the day."""
    hour = (at_ns / SECOND / 3600) % 24
    return 0.55 + 0.45 * (1 + math.cos(2 * math.pi * (hour - 15) / 24)) / 2


def default_end(now: datetime | None = None) -> datetime:
    """The current minute (UTC): the seed ends where the dashboards' "last N hours" end."""
    now = now or datetime.now(UTC)
    return now.replace(second=0, microsecond=0)


@dataclass(slots=True)
class _Position:
    profile: str
    symbol: str
    quote: float
    opened: int
    stop: float
    take: float
    ret: float = 0.0


class _Open:
    """A span being built: children run one after another from its start (``cursor``)."""

    def __init__(
        self,
        gen: "_Generator",
        trace_id: bytes,
        parent: "_Open | None",
        service: str,
        name: str,
        kind: SpanKind,
        start: int,
        attributes: Attributes | None = None,
    ) -> None:
        self.gen, self.trace_id, self.parent = gen, trace_id, parent
        self.service, self.name, self.kind, self.start = service, name, kind, start
        self.span_id = gen.random_bytes(8)
        self.attributes: dict[str, AttributeValue] = dict(attributes or {})
        self.cursor = start + gen.jitter(0.3, 0.2) * MS
        self.error: str | None = None

    def open(
        self,
        component: str,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes | None = None,
    ) -> "_Open":
        return _Open(
            self.gen, self.trace_id, self, f"{NAMESPACE}.{component}", name, kind, self.cursor,
            attributes,
        )  # fmt: skip

    def leaf(
        self,
        component: str,
        name: str,
        millis: float,
        *,
        kind: SpanKind = SpanKind.CLIENT,
        attributes: Attributes | None = None,
        error: str | None = None,
    ) -> Span:
        child = self.open(component, name, kind, attributes)
        child.cursor += int(millis * MS)
        child.error = error
        return child.close()

    def log(self, event: str, level: str = "debug", **fields: object) -> None:
        self.gen.agent_log(self.cursor, event, level, self, fields)

    def close(self, extra_ms: float = 0.2) -> Span:
        end = self.cursor + int(extra_ms * MS)
        span = Span(
            self.trace_id,
            self.span_id,
            self.parent.span_id if self.parent else b"",
            self.service,
            self.name,
            self.kind,
            self.start,
            end,
            self.attributes,
            self.error,
        )
        self.gen.spans.append(span)
        if self.parent is not None:
            self.parent.cursor = end + self.gen.jitter(0.2, 0.1) * MS
        return span


class _Generator:
    def __init__(self, seed: int, start: int, end: int) -> None:
        self.rng = random.Random(seed)  # noqa: S311 - synthetic data, reproducible on purpose
        self.seed, self.start, self.end = seed, start, end
        self.spans: list[Span] = []
        self.logs: list[LogRecord] = []
        self.gauges: dict[tuple[str, tuple[tuple[str, str], ...]], Metric] = {}
        self.increments: dict[str, list[tuple[int, Mapping[str, str | bool], float]]] = defaultdict(
            list
        )
        self.instance = self.uuid()
        # portfolio: realized results before the window, split per profile
        self.realized = {p: self.rng.uniform(-45.0, 25.0) for p in PROFILES}
        self.positions: list[_Position] = []
        self.peak = CAPITAL + max(0.0, sum(self.realized.values())) + self.rng.uniform(5, 40)
        self.day_start = CAPITAL + sum(self.realized.values()) + self.rng.uniform(-4, 4)
        self.states = dict.fromkeys(SCOPES, "running")
        self.clock_offset = self.rng.uniform(150, 450)
        self.weight = 0
        self.recent_calls: list[tuple[int, bool]] = []  # (minute, failed) for the 5-min error rate
        self.exchange_errors: Counter[int] = Counter()
        self.exchange_requests = 0
        self.job_failures = 0
        self.llm_cost = 0.0
        self.llm_tokens = 0
        self.llm_calls = 0
        self.cycles: Counter[str] = Counter()
        self.entries: Counter[str] = Counter()
        self.exits: Counter[str] = Counter()
        self.state_changes: set[str] = set()
        self.minutes = (end - start) // MINUTE
        # guaranteed rare events (minute offsets inside the window)
        self.forced_failure = self.rng.randrange(self.minutes // 4, 3 * self.minutes // 4)
        self.forced_http = self.rng.randrange(self.minutes // 5, 4 * self.minutes // 5)
        pause = self.rng.randrange(self.minutes // 5, self.minutes // 2)
        self.global_pause = (pause, pause + self.rng.randrange(60, 181))
        halt = self.rng.randrange(self.minutes // 2, 4 * self.minutes // 5)
        self.profile_halt = (halt, halt + self.rng.randrange(30, 91))
        self.last: dict[str, float] = {}
        self.container_totals: dict[tuple[object, ...], float] = {}

    # ------------------------------------------------------------ helpers
    def random_bytes(self, size: int) -> bytes:
        return self.rng.getrandbits(size * 8).to_bytes(size, "big")

    def uuid(self) -> str:
        raw = self.random_bytes(16).hex()
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    def jitter(self, mean: float, spread: float) -> int:
        return max(0, round(self.rng.gauss(mean, spread)))

    def lognormal_ms(self, median: float, sigma: float = 0.35) -> float:
        return median * math.exp(self.rng.gauss(0, sigma))

    def poisson(self, lam: float) -> int:
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def trace(
        self, component: str, name: str, at: int, attributes: Attributes | None = None
    ) -> _Open:
        service = f"{NAMESPACE}.{component}"
        return _Open(
            self, self.random_bytes(16), None, service, name, SpanKind.INTERNAL, at, attributes
        )

    @staticmethod
    def iso(at: int) -> str:
        moment = datetime.fromtimestamp(at / SECOND, UTC)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def agent_log(
        self, at: int, event: str, level: str, span: _Open | None, fields: Mapping[str, object]
    ) -> None:
        """A structlog line as Alloy delivers it: JSON body, fields flattened into attributes."""
        record: dict[str, object] = {**fields, "event": event}
        if span is not None:
            record["trace_id"] = span.trace_id.hex()
            record["span_id"] = span.span_id.hex()
        record["level"] = level
        record["timestamp"] = self.iso(at)
        attributes: dict[str, AttributeValue] = {"service": "agent"}
        for name, value in _flatten(record):
            attributes[name] = value
        self.logs.append(
            LogRecord(
                at,
                "agent",
                level,
                json.dumps(record),
                attributes,
                span.trace_id if span else b"",
                span.span_id if span else b"",
            )
        )

    def gauge(
        self,
        name: str,
        unit: str,
        at: int,
        value: float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.gauge_on(name, unit, self.agent_resource(), at, value, attributes or {})

    def gauge_on(
        self,
        name: str,
        unit: str,
        resource: tuple[tuple[str, str], ...],
        at: int,
        value: float,
        attributes: Mapping[str, str | bool],
        *,
        kind: str = "gauge",
        monotonic: bool = False,
    ) -> None:
        metric = self.gauges.get((name, resource))
        if metric is None:
            metric = Metric(name, unit, kind, resource, monotonic=monotonic, start=self.start)
            self.gauges[(name, resource)] = metric
        metric.points.append(Point(at, value, attributes))

    def count(self, name: str, at: int, amount: float, **attributes: str | bool) -> None:
        self.increments[name].append((at, attributes, amount))

    def agent_resource(self) -> tuple[tuple[str, str], ...]:
        return (
            ("service.name", NAMESPACE),
            ("service.namespace", NAMESPACE),
            ("service.version", VERSION),
            ("deployment.environment", ENVIRONMENT),
            ("deployment.environment.name", ENVIRONMENT),
            ("service.instance.id", self.instance),
            ("telemetry.sdk.language", "python"),
            ("telemetry.sdk.name", "opentelemetry"),
        )

    # ------------------------------------------------------------ building blocks
    def exchange(
        self, parent: _Open, path: str, *, method: str = "GET", signed: bool = False,
        weight: int = 2, median_ms: float = 300.0, fail: int | None = None,
    ) -> bool:  # fmt: skip
        """A Binance REST call (span + ``rest.request`` log); ``fail``: forced HTTP status."""
        busy = activity(parent.cursor)
        millis = self.lognormal_ms(median_ms * (0.75 + 0.5 * busy))
        status = fail
        if status is None and self.rng.random() < 0.0015 * busy:
            status = self.rng.choices(list(HTTP_FAILURES), list(HTTP_FAILURES.values()))[0]
        status = status or 200
        self.weight += weight
        attributes: dict[str, AttributeValue] = {
            "http.request.method": method,
            "url.path": path,
            "server.address": "demo-api.binance.com",
            "peer.service": "binance",
            "http.response.status_code": status,
            "trade_agent.used_weight_1m": self.weight,
            "trade_agent.signed": signed,
        }
        error = f"HTTP {status}" if status >= 400 else None
        span = parent.open("exchange", f"{method} {path}", SpanKind.CLIENT, attributes)
        span.cursor += int(millis * MS)
        span.error = error
        span.log(
            "rest.request", method=method, path=path, status=status,
            elapsed_ms=round(millis, 1), weight_1m=self.weight, signed=signed,
        )  # fmt: skip
        span.close()
        self.exchange_requests += 1
        minute = (parent.cursor - self.start) // MINUTE
        self.recent_calls.append((minute, error is not None))
        if error:
            self.exchange_errors[status] += 1
        return error is None

    def db(self, parent: _Open, operation: str, table: str, median_ms: float = 0.6) -> None:
        query = {  # the db.query.text attribute (never executed)
            "SELECT": f"SELECT {table}.* \nFROM {table}",
            "INSERT": f"INSERT INTO {table} VALUES ($1)",  # noqa: S608
            "UPDATE": f"UPDATE {table} SET updated_at = now()",  # noqa: S608
        }[operation]
        parent.leaf(
            "db",
            f"{operation} {table}",
            self.lognormal_ms(median_ms, 0.5),
            attributes={
                "db.system.name": "postgresql",
                "db.operation.name": operation,
                "db.collection.name": table,
                "db.query.text": query,
                "server.address": "postgres",
                "peer.service": "postgres",
            },
        )

    def job(self, name: str, at: int) -> _Open:
        return self.trace("runtime", f"job {name}", at)

    def finish_job(self, job: _Open, name: str, failure: str | None = None) -> None:
        if failure is not None:
            job.error = failure.split("(", 1)[0]
            job.log("runtime.task_failed", "error", job=name, error=failure)
            self.job_failures += 1
        span = job.close()
        elapsed = (span.end - span.start) // MS
        self.agent_log(span.end, "runtime.job", "debug", None, {"job": name, "elapsed_ms": elapsed})

    # ------------------------------------------------------------ portfolio
    def unrealized(self, profile: str | None = None) -> float:
        return sum(p.quote * p.ret for p in self.positions if profile in (None, p.profile))

    def equity(self) -> float:
        return CAPITAL + sum(self.realized.values()) + self.unrealized()

    def set_state(self, scope: str, state: str, at: int, source: str, parent: _Open) -> None:
        if self.states[scope] == state:
            return
        self.states[scope] = state
        self.state_changes.add(f"{scope} → {state}")
        self.count("trade_agent.risk.state_changes", at, 1, scope=scope, state=state, source=source)
        level = "warning" if state != "running" else "info"
        parent.log("risk.state_changed", level, scope=scope, state=state, source=source)

    def close_position(self, position: _Position, at: int, reason: str, parent: _Open) -> None:
        pnl = position.quote * position.ret
        self.realized[position.profile] += pnl
        self.positions.remove(position)
        self.db(parent, "UPDATE", "positions")
        parent.log(
            "position.closed", "info", symbol=position.symbol, state="closed", reason=reason,
            pnl=f"{pnl:.6f}", profile=position.profile,
        )  # fmt: skip

    # ------------------------------------------------------------ jobs
    def check_risk(self, minute: int, at: int) -> None:
        job = self.job("check_risk", at)
        snapshot = job.open("risk", "risk.snapshot")
        failure = None
        forced = minute == self.forced_failure
        if forced or self.rng.random() < 0.0007:
            ok = self.exchange(snapshot, "/api/v3/ticker/bookTicker", median_ms=1500, fail=503)
            failure = (
                'BinanceUnknownStatusError("resultado desconhecido em GET '
                '/api/v3/ticker/bookTicker: HTTP 503")'
            )
            assert not ok
        else:
            for _ in range(1 + (self.rng.random() < 0.4 * activity(at))):
                self.exchange(snapshot, "/api/v3/ticker/bookTicker", median_ms=900)
            self.db(snapshot, "SELECT", "positions")
            self.db(snapshot, "SELECT", "checkpoints")
            self.db(snapshot, "SELECT", "checkpoints")
            self.db(snapshot, "INSERT", "checkpoints")
            snapshot.log(
                "risk.snapshot", equity=f"{self.equity():.6f}", day_start=f"{self.day_start:.6f}",
                peak=f"{self.peak:.6f}", positions=len(self.positions),
            )  # fmt: skip
        snapshot.close()
        if failure is None:
            apply = job.open("risk", "risk.apply")
            self.risk_rules(minute, at, apply)
            apply.close()
            observe = job.open("telemetry", "telemetry.observe")
            if minute % 5 == 3:
                self.db(observe, "INSERT", "telemetry_snapshots")
                observe.log("telemetry.recorded", equity=f"{self.equity():.6f}")
            observe.close()
        self.finish_job(job, "check_risk", failure)

    def risk_rules(self, minute: int, at: int, parent: _Open) -> None:
        start, stop = self.global_pause
        if minute == start:
            self.set_state("global", "paused", at, "gatilho", parent)
        elif minute == stop:
            self.set_state("global", "running", at, "manual", parent)
        start, stop = self.profile_halt
        if minute == start:
            self.set_state("momentum_alpha", "halted", at, "gatilho", parent)
        elif minute == stop:
            self.set_state("momentum_alpha", "running", at, "manual", parent)

    def reconcile(self, at: int) -> None:
        job = self.job("reconcile", at)
        run = job.open("reconcile", "reconcile.all")
        self.exchange(run, "/api/v3/openOrderList", signed=True, weight=6)
        self.db(run, "SELECT", "positions")
        self.db(run, "SELECT", "intents")
        for position in list(self.positions):
            sync = run.open("execution", "position.sync")
            self.exchange(sync, "/api/v3/orderList", signed=True, weight=4, median_ms=600)
            self.exchange(sync, "/api/v3/order", signed=True, weight=4)
            if position.ret <= -position.stop:
                self.close_position(position, sync.cursor, "stop_loss", sync)
            elif position.ret >= position.take:
                self.close_position(position, sync.cursor, "take_profit", sync)
            else:
                sync.log("position.sync", symbol=position.symbol, verdict="protected")
            sync.close()
        run.log("reconcile.done", positions=len(self.positions), errors=0)
        run.close()
        self.finish_job(job, "reconcile")

    def sync_time(self, at: int) -> None:
        job = self.job("sync_time", at)
        self.exchange(job, "/api/v3/time", weight=1)
        job.log("rest.clock_synced", offset_ms=round(self.clock_offset), round_trip_ms=300)
        self.finish_job(job, "sync_time")

    def ingest(self, at: int) -> None:
        job = self.job("ingest", at)
        ingest = job.open("research", "research.ingest")
        collect = ingest.open("research", "research.collect")
        items = 0
        for source in NEWS_SOURCES:
            median = 3_900 if source == "derivatives" else 200
            collect.leaf(
                "research", f"source {source}", self.lognormal_ms(median), kind=SpanKind.CLIENT
            )
            found = self.rng.randrange(5, 40)
            items += found
            collect.log("research.source", source=source, items=found)
        collect.close()
        self.db(ingest, "INSERT", "news_items", 3.0)
        self.db(ingest, "UPDATE", "news_items", 0.4)
        ingest.log("research.ingested", collected=items, new=self.rng.randrange(0, 12))
        ingest.close()
        self.finish_job(job, "ingest")

    def research(self, parent: _Open) -> None:
        cycle = parent.open("research", "research.cycle")
        cost = 0.0
        for model, purpose, tokens_in, tokens_out, seconds in LLM_CALLS:
            step = cycle.open(
                "research", "analyst.triage" if purpose == "triage" else "analyst.analyze"
            )
            used_in = round(tokens_in * self.rng.uniform(0.7, 1.3))
            used_out = round(tokens_out * self.rng.uniform(0.6, 1.4))
            price_in, price_out = LLM_PRICES[model]
            call_cost = round((used_in * price_in + used_out * price_out) / 1e6, 6)
            step.log("llm.request", purpose=purpose, model=model, max_tokens=16_000)
            step.leaf(
                "llm",
                f"chat {model}",
                self.lognormal_ms(seconds * 1000, 0.25),
                attributes={
                    "gen_ai.provider.name": "anthropic",
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": model,
                    "peer.service": "anthropic",
                    "trade_agent.purpose": purpose,
                    "trade_agent.cost_usd": f"{call_cost:.6f}",
                    "gen_ai.usage.input_tokens": used_in,
                    "gen_ai.usage.output_tokens": used_out,
                    "gen_ai.request.max_tokens": 16_000,
                },
            )
            step.log(
                "llm.response", purpose=purpose, model=model, input_tokens=used_in,
                output_tokens=used_out, cost_usd=f"{call_cost:.6f}",
            )  # fmt: skip
            step.close()
            at = step.cursor
            self.count("trade_agent.llm.cost", at, call_cost, model=model, purpose=purpose)
            for direction, used in (("input", used_in), ("output", used_out)):
                self.count(
                    "trade_agent.llm.tokens", at, used, model=model, purpose=purpose,
                    direction=direction,
                )  # fmt: skip
            self.llm_calls += 1
            self.llm_cost += call_cost
            self.llm_tokens += used_in + used_out
            cost += call_cost
        self.db(cycle, "INSERT", "llm_usage")
        self.db(cycle, "INSERT", "research_reports", 2.7)
        cycle.log("research.cycle", status="ok", cost_usd=f"{cost:.6f}")
        cycle.close()

    def decide(self, profile: str, at: int, *, research: bool, last_cycle: bool) -> None:
        job = self.job(f"decide_{profile}", at)
        cycle = job.open("decision", "decision.cycle")
        state = "paused" if self.states["global"] != "running" else self.states[profile]
        cycle.log("decision.cycle_start", profile=profile, state=state, universe=95, dry_run=False)
        universe = cycle.open("decision", "universe.get")
        self.exchange(universe, "/api/v3/exchangeInfo", weight=20, median_ms=2300)
        self.exchange(universe, "/api/v3/ticker/24hr", weight=40, median_ms=380)
        universe.close()
        evaluated = self.rng.randrange(18, 32)
        for _ in range(evaluated):
            self.exchange(cycle, "/api/v3/klines", weight=2, median_ms=290)
        if research:
            self.research(cycle)
        opened = exits = 0
        mine = [p for p in self.positions if p.profile == profile]
        if state == "running":
            # rule exits: the weakest position after a few hours (at least one in the window)
            for position in mine:
                aged = cycle.cursor - position.opened > 6 * 3600 * SECOND
                forced = last_cycle and not self.exits
                if (aged and position.ret < -0.01) or forced:
                    self.close_position(position, cycle.cursor, "rule_exit", cycle)
                    exits += 1
                    break
            wanted = self.rng.choice((0, 1, 1, 2)) if self.entries.total() else 1
            for _ in range(min(wanted, 3 - len(mine) + exits)):
                self.open_position(profile, cycle)
                opened += 1
        cycle.log(
            "decision.cycle", "info", profile=profile, state=state, evaluated=evaluated,
            dry_run=False, research="ok" if research else "sem candidatos (leitura anterior)",
        )  # fmt: skip
        cycle.close()
        self.finish_job(job, f"decide_{profile}")
        self.cycles[profile] += 1
        self.count(
            "trade_agent.decision.cycles", at, 1, profile=profile, state=state, dry_run=False
        )
        if opened:
            self.entries[profile] += opened
            self.count("trade_agent.decision.entries", at, opened, profile=profile, dry_run=False)
        if exits:
            self.exits[profile] += exits
            self.count("trade_agent.decision.exits", at, exits, profile=profile, dry_run=False)

    def open_position(self, profile: str, parent: _Open) -> None:
        symbol = self.rng.choice(SYMBOLS)
        position = _Position(
            profile, symbol, round(self.rng.uniform(80, 160), 2), parent.cursor,
            stop=self.rng.uniform(0.03, 0.06), take=self.rng.uniform(0.06, 0.12),
        )  # fmt: skip
        self.positions.append(position)
        opening = parent.open("execution", "position.open")
        self.db(opening, "INSERT", "positions")
        self.db(opening, "INSERT", "intents")
        submit = opening.open("execution", "order_list.submit")
        self.exchange(submit, "/api/v3/orderList/opoco", method="POST", signed=True, weight=1)
        self.db(submit, "INSERT", "exchange_orders")
        submit.close()
        opening.log("position.protected", "info", symbol=symbol, state="protected", profile=profile)
        opening.close()

    # ------------------------------------------------------------ minute by minute
    def run(self) -> Telemetry:
        # one position opened before the window
        quote = round(self.rng.uniform(80, 160), 2)
        before = self.start - 3 * 3600 * SECOND
        self.positions.append(_Position("swing_trend", "ETHUSDT", quote, before, 0.05, 0.10))
        for minute in range(self.minutes):
            at = self.start + minute * MINUTE
            self.weight = self.weight // 3  # the 1-minute window rolls over
            self.market(minute, at)
            self.check_risk(minute, at + 40 * SECOND)
            if minute % 5 == 2:
                self.reconcile(at + 38 * SECOND)
            if minute % 10 == 9:
                self.sync_time(at + 50 * SECOND)
            if minute % 15 == 12:
                self.ingest(at + 28 * SECOND)
            if minute == self.forced_http:  # a rate limit, at least once
                job = self.job("sync_time", at + 55 * SECOND)
                self.exchange(job, "/api/v3/time", weight=1, fail=429)
                job.log("rest.rate_limited", "warning", path="/api/v3/time", status=429)
                self.finish_job(job, "sync_time")
            moment = datetime.fromtimestamp(at / SECOND, UTC)
            if moment.hour % 4 == 0 and moment.minute == 0:
                last = minute + 240 >= self.minutes
                for index, profile in enumerate(PROFILES):
                    self.decide(
                        profile,
                        at + (20 + index * 5) * SECOND,
                        research=index == 0,
                        last_cycle=last,
                    )
            self.background_logs(at)
            self.agent_gauges(minute, at + 41 * SECOND)
            self.container_metrics(minute, at + 30 * SECOND)
        return self.telemetry()

    def market(self, minute: int, at: int) -> None:
        moment = datetime.fromtimestamp(at / SECOND, UTC)
        if moment.hour == 0 and moment.minute == 0:
            self.day_start = self.equity()
        for position in self.positions:
            position.ret += self.rng.gauss(0.00005, 0.0011 * (0.7 + 0.6 * activity(at)))
        self.clock_offset += self.rng.gauss(0, 12)
        if self.rng.random() < 0.002:
            self.clock_offset = self.rng.uniform(100, 700)  # a jump (NTP correction)
        self.clock_offset = min(max(self.clock_offset, -200), 1_400)
        if self.rng.random() < 0.015 * activity(at):
            self.agent_log(
                at + self.rng.randrange(60) * SECOND, "telegram.poll_failed", "warning", None,
                {"error": "getUpdates: ReadTimeout"},
            )  # fmt: skip

    def background_logs(self, at: int) -> None:
        """Lines of the other compose services (Alloy sends every project container)."""
        busy = activity(at)
        rates = {
            "grafana": (2.8 * busy, (("info", 0.985), ("warn", 0.01), ("error", 0.005))),
            "postgres": (0.4, (("info", 0.995), ("error", 0.005))),
            "loki": (0.3 * busy, (("info", 0.99), ("warn", 0.01))),
            "alloy": (0.04, (("warn", 1.0),)),
            "jaeger": (0.03, (("info", 0.9), ("warn", 0.1))),
            "container-metrics": (0.01, (("warn", 1.0),)),
        }
        for service, (lam, levels) in rates.items():
            for _ in range(self.poisson(lam)):
                when = at + self.rng.randrange(60 * 1000) * MS
                level = self.rng.choices([lv for lv, _ in levels], [w for _, w in levels])[0]
                body = _service_line(service, level, self.iso(when), self.rng)
                attributes = {"service": service, "level": level}
                self.logs.append(LogRecord(when, service, level, body, attributes))

    def agent_gauges(self, minute: int, at: int) -> None:
        equity = self.equity()
        self.peak = max(self.peak, equity)
        cutoff = minute - 5
        self.recent_calls = [c for c in self.recent_calls if c[0] > cutoff]
        failed = sum(1 for _, bad in self.recent_calls if bad)
        values = {
            "trade_agent.equity": ("USD", equity),
            "trade_agent.equity.day_start": ("USD", self.day_start),
            "trade_agent.equity.peak": ("USD", self.peak),
            "trade_agent.drawdown": ("%", (self.peak - equity) / self.peak * 100),
            "trade_agent.pnl.daily": ("USD", equity - self.day_start),
            "trade_agent.pnl.realized": ("USD", sum(self.realized.values())),
            "trade_agent.pnl.unrealized": ("USD", self.unrealized()),
            "trade_agent.exposure": ("USD", sum(p.quote for p in self.positions)),
            "trade_agent.positions.active": ("{position}", float(len(self.positions))),
            "trade_agent.binance.error_rate": ("1", failed / max(1, len(self.recent_calls))),
            "trade_agent.binance.weight_used_1m": ("{weight}", float(self.weight)),
            "trade_agent.clock.offset": ("ms", float(round(self.clock_offset))),
        }
        for name, (unit, value) in values.items():
            self.gauge(name, unit, at, value)
            self.last[name] = value
        scope_pnl = {"global": sum(self.realized.values()) + self.unrealized()}
        scope_pnl |= {p: self.realized[p] + self.unrealized(p) for p in PROFILES}
        for scope in SCOPES:
            self.gauge("trade_agent.pnl.total", "USD", at, scope_pnl[scope], {"scope": scope})
            code = float(STATE_CODES[self.states[scope]])
            self.gauge("trade_agent.risk.state", "1", at, code, {"scope": scope})
        self.last["trade_agent.risk.state"] = max(STATE_CODES[s] for s in self.states.values())

    def container_metrics(self, minute: int, at: int) -> None:
        busy = activity(at)
        decision = datetime.fromtimestamp(at / SECOND, UTC).hour % 4 == 0 and minute % 60 < 5
        for service, (cpu, memory, network, disk) in CONTAINERS.items():
            resource = _container_resource(service)
            load = busy * (3.0 if decision and service == "agent" else 1.0)
            usage = memory * 1024**2 * (1 + 0.08 * math.sin(minute / 97 + len(service)))
            usage *= 1 + self.rng.gauss(0, 0.01)
            self.gauge_on(
                "container.cpu.utilization", "1", resource, at,
                max(0.01, cpu * load * (1 + self.rng.gauss(0, 0.15))), {},
            )  # fmt: skip
            self.gauge_on(
                "container.memory.usage.total", "By", resource, at, usage, {}, kind="sum",
                monotonic=False,
            )  # fmt: skip
            self.gauge_on(
                "container.memory.percent", "1", resource, at, usage / MEMORY_LIMIT * 100, {}
            )
            for direction, share in (("rx", 0.55), ("tx", 0.45)):
                self.count_on(
                    f"container.network.io.usage.{direction}_bytes", resource, at,
                    network * 1024 * share * load * self.rng.uniform(0.7, 1.3), interface="eth0",
                )  # fmt: skip
            for operation, share in (("read", 0.3), ("write", 0.7)):
                amount = disk * 1024 * share * load * self.rng.uniform(0.5, 1.5)
                for op in (operation, "total"):
                    self.count_on(
                        "container.blockio.io_service_bytes_recursive", resource, at, amount,
                        operation=op, device_major="8", device_minor="48",
                    )  # fmt: skip

    def count_on(
        self, name: str, resource: tuple[tuple[str, str], ...], at: int, amount: float,
        **attributes: str,
    ) -> None:  # fmt: skip
        """docker_stats counters: cumulative since the container started (no zero needed)."""
        key = (name, resource)
        metric = self.gauges.get(key)
        series = tuple(sorted(attributes.items()))
        if metric is None:
            metric = Metric(name, "By", "sum", resource, monotonic=True, start=self.start)
            self.gauges[key] = metric
        totals = self.container_totals.get((key, series)) or self.rng.uniform(1e6, 5e8)
        totals += amount
        self.container_totals[(key, series)] = totals
        metric.points.append(Point(at, totals, attributes))

    # ------------------------------------------------------------ output
    def counters(self) -> list[Metric]:
        """Cumulative sums, exported every minute like the SDK, starting at zero on the window's
        start: an ``increase`` over the window then adds up exactly what was recorded."""
        units = {
            "trade_agent.llm.cost": "USD",
            "trade_agent.llm.tokens": "{token}",
            "trade_agent.decision.cycles": "{cycle}",
            "trade_agent.decision.entries": "{position}",
            "trade_agent.decision.exits": "{position}",
            "trade_agent.risk.state_changes": "{change}",
        }
        metrics = []
        for name, unit in units.items():
            metric = Metric(name, unit, "sum", self.agent_resource(), start=self.start)
            by_series: dict[tuple[tuple[str, str | bool], ...], list[tuple[int, float]]] = (
                defaultdict(list)
            )
            for at, attributes, amount in self.increments[name]:
                by_series[tuple(sorted(attributes.items()))].append((at, amount))
            for series, events in by_series.items():
                events.sort()
                total, index = 0.0, 0
                for minute in range(self.minutes):
                    export = self.start + minute * MINUTE + 41 * SECOND
                    while index < len(events) and events[index][0] <= export:
                        total += events[index][1]
                        index += 1
                    metric.points.append(Point(export, total, dict(series)))
            metrics.append(metric)
        return metrics

    def telemetry(self) -> Telemetry:
        metrics = list(self.gauges.values()) + self.counters()
        error_levels = {"error", "critical", "fatal"}
        problem_levels = {"warn", "warning", *error_levels}
        problems = [log for log in self.logs if log.severity in problem_levels]
        jobs = [s for s in self.spans if s.name.startswith("job ")]
        summary = Summary(
            seed=self.seed,
            start=self.start,
            end=self.end,
            spans=len(self.spans),
            logs=len(self.logs),
            metric_points=sum(len(m.points) for m in metrics),
            exchange_requests=self.exchange_requests,
            exchange_http_errors=dict(sorted(self.exchange_errors.items())),
            job_failures=self.job_failures,
            error_logs=sum(1 for log in self.logs if log.severity in error_levels),
            llm_calls=self.llm_calls,
            llm_cost=round(self.llm_cost, 6),
            llm_tokens=self.llm_tokens,
            decision_cycles=dict(self.cycles),
            entries=dict(self.entries),
            exits=dict(self.exits),
            state_changes=sorted(self.state_changes),
            containers=sorted(CONTAINERS),
            services=sorted({s.service for s in self.spans}),
            jobs=sorted({s.name for s in jobs}),
            failed_jobs=sorted({s.name for s in jobs if s.error}),
            llm_series=sorted(f"{model} · {purpose}" for model, purpose, *_ in LLM_CALLS),
            log_services=sorted({log.service for log in self.logs}),
            problem_logs=sorted({f"{log.service} {log.severity}" for log in problems}),
            agent_problems=sum(1 for log in problems if log.service == "agent"),
            last=dict(self.last),
        )
        return Telemetry(self.spans, self.logs, metrics, summary)


def generate(seed: int = 7, hours: float = 24, end: datetime | None = None) -> Telemetry:
    """``hours`` of telemetry ending at ``end`` (default: the current minute, UTC)."""
    if hours < 4:
        raise ValueError("at least 4 hours: the decision cycles run every 4 hours")
    stop = int(default_end(end).timestamp()) * SECOND
    start = stop - int(timedelta(hours=hours).total_seconds()) * SECOND
    return _Generator(seed, start, stop).run()


def _flatten(value: object, prefix: str = "") -> Iterator[tuple[str, AttributeValue]]:
    """Like OTTL ``flatten``: nested keys joined by ``.``, lists by their index."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v, f"{prefix}{k}." if prefix or k else "")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _flatten(v, f"{prefix}{i}.")
    elif value is not None:
        name = prefix.rstrip(".")
        yield name, value if isinstance(value, str | bool | int | float) else str(value)


def _service_line(service: str, level: str, at: str, rng: random.Random) -> str:
    if service == "postgres":
        kind = {"info": "LOG", "error": "ERROR"}[level]
        return f"{at} UTC [{rng.randrange(100, 999)}] {kind}:  checkpoint complete"
    if service in {"jaeger", "container-metrics"}:
        return json.dumps({"level": level, "ts": at, "msg": "exporting batch"})
    return f'logger=context t={at} level={level} msg="Request completed" status=200'


def _container_resource(service: str) -> tuple[tuple[str, str], ...]:
    name = service if service.startswith("signoz-") else f"trade-agent-{service}-1"
    container_id = "".join(f"{ord(c):02x}" for c in service)[:12].ljust(12, "0")
    return (
        ("compose.project", NAMESPACE),
        ("compose.service", service),
        ("container.name", name),
        ("container.id", container_id),
        ("container.hostname", container_id),
        ("container.runtime", "docker"),
        ("service.namespace", NAMESPACE),
    )
