"""Sends the seed over OTLP/HTTP (protobuf) to an ingester: the same path the real data takes.

The SigNoz collector (signoz-otel-collector) writes the records with their own timestamps, so a
whole day in the past goes through the real receivers, processors and ClickHouse exporters,
and lands in the tables with the schema the migrator created. Nothing is written to
ClickHouse directly.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence

import httpx
from google.protobuf.message import Message  # type: ignore[import-untyped]
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as OtlpLog
from opentelemetry.proto.logs.v1.logs_pb2 import ResourceLogs, ScopeLogs
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AGGREGATION_TEMPORALITY_CUMULATIVE,
    Gauge,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
    Sum,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import Metric as OtlpMetric
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Status
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

from tests.support.signoz_smoke.seed import (
    ENVIRONMENT,
    NAMESPACE,
    VERSION,
    AttributeValue,
    LogRecord,
    Metric,
    Span,
    Telemetry,
)

BATCH = 4_000  # records per request
SEVERITY_NUMBERS = {"debug": 5, "info": 9, "warn": 13, "warning": 13, "error": 17, "fatal": 21}
SCOPE = InstrumentationScope(name="trade_agent")
DOCKER_SCOPE = InstrumentationScope(
    name="github.com/open-telemetry/opentelemetry-collector-contrib/receiver/dockerstatsreceiver",
    version="0.162.0",
)


def _value(value: AttributeValue) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    return AnyValue(string_value=value)


def _attributes(attributes: Mapping[str, AttributeValue]) -> list[KeyValue]:
    return [KeyValue(key=k, value=_value(v)) for k, v in attributes.items()]


def _resource(attributes: Mapping[str, AttributeValue]) -> Resource:
    return Resource(attributes=_attributes(attributes))


def _span_resource(service: str) -> dict[str, AttributeValue]:
    """The resource of each component's tracer provider (``trade_agent.tracing.Tracing``)."""
    return {
        "service.name": service,
        "service.namespace": NAMESPACE,
        "service.version": VERSION,
        "deployment.environment.name": ENVIRONMENT,
        "deployment.environment": ENVIRONMENT,
        "telemetry.sdk.language": "python",
        "telemetry.sdk.name": "opentelemetry",
    }


def _chunks[T](items: Sequence[T], size: int = BATCH) -> Iterator[Sequence[T]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def trace_requests(spans: Sequence[Span]) -> Iterator[ExportTraceServiceRequest]:
    for chunk in _chunks(spans):
        by_service: dict[str, list[OtlpSpan]] = defaultdict(list)
        for span in chunk:
            status = (
                Status(code=Status.STATUS_CODE_ERROR, message=span.error)
                if span.error
                else Status()
            )
            by_service[span.service].append(
                OtlpSpan(
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_id,
                    name=span.name,
                    kind=span.kind.value,  # type: ignore[arg-type]
                    start_time_unix_nano=span.start,
                    end_time_unix_nano=span.end,
                    attributes=_attributes(span.attributes),
                    status=status,
                )
            )
        yield ExportTraceServiceRequest(
            resource_spans=[
                ResourceSpans(
                    resource=_resource(_span_resource(service)),
                    scope_spans=[ScopeSpans(scope=SCOPE, spans=items)],
                )
                for service, items in by_service.items()
            ]
        )


def log_requests(logs: Sequence[LogRecord]) -> Iterator[ExportLogsServiceRequest]:
    """Each compose service is a resource, like Alloy's ``otelcol.receiver.loki`` output."""
    for chunk in _chunks(logs):
        by_service: dict[str, list[OtlpLog]] = defaultdict(list)
        for log in chunk:
            by_service[log.service].append(
                OtlpLog(
                    time_unix_nano=log.time,
                    observed_time_unix_nano=log.time,
                    severity_text=log.severity,
                    severity_number=SEVERITY_NUMBERS.get(log.severity, 0),  # type: ignore[arg-type]
                    body=AnyValue(string_value=log.body),
                    attributes=_attributes(log.attributes),
                    trace_id=log.trace_id,
                    span_id=log.span_id,
                )
            )
        yield ExportLogsServiceRequest(
            resource_logs=[
                ResourceLogs(
                    resource=_resource({"service.name": service, "service.namespace": NAMESPACE}),
                    scope_logs=[ScopeLogs(log_records=items)],
                )
                for service, items in by_service.items()
            ]
        )


def _metric(metric: Metric, points: Iterable[NumberDataPoint]) -> OtlpMetric:
    if metric.kind == "gauge":
        return OtlpMetric(name=metric.name, unit=metric.unit, gauge=Gauge(data_points=points))
    total = Sum(
        data_points=points,
        aggregation_temporality=AGGREGATION_TEMPORALITY_CUMULATIVE,
        is_monotonic=metric.monotonic,
    )
    return OtlpMetric(name=metric.name, unit=metric.unit, sum=total)


def metric_requests(metrics: Sequence[Metric]) -> Iterator[ExportMetricsServiceRequest]:
    """One request per metric and slice of points (the SDK would send one per minute)."""
    for metric in metrics:
        resource = dict(metric.resource)
        scope = DOCKER_SCOPE if metric.name.startswith("container.") else SCOPE
        for chunk in _chunks(metric.points):
            points = [
                NumberDataPoint(
                    time_unix_nano=point.time,
                    start_time_unix_nano=metric.start if metric.kind == "sum" else 0,
                    as_double=point.value,
                    attributes=_attributes(point.attributes),
                )
                for point in chunk
            ]
            yield ExportMetricsServiceRequest(
                resource_metrics=[
                    ResourceMetrics(
                        resource=_resource(resource),
                        scope_metrics=[
                            ScopeMetrics(scope=scope, metrics=[_metric(metric, points)])
                        ],
                    )
                ]
            )


def send(telemetry: Telemetry, endpoint: str, *, timeout: float = 60) -> int:
    """Posts everything to ``endpoint`` (e.g. ``http://127.0.0.1:4318``); returns the requests."""
    plan: list[tuple[str, Iterator[Message]]] = [
        ("/v1/metrics", metric_requests(telemetry.metrics)),
        ("/v1/traces", trace_requests(telemetry.spans)),
        ("/v1/logs", log_requests(telemetry.logs)),
    ]
    sent = 0
    headers = {"Content-Type": "application/x-protobuf"}
    with httpx.Client(base_url=endpoint.rstrip("/"), timeout=timeout, headers=headers) as http:
        for path, requests in plan:
            for request in requests:
                response = http.post(path, content=request.SerializeToString())
                if response.is_error:
                    raise RuntimeError(f"OTLP {path}: HTTP {response.status_code} {response.text}")
                sent += 1
    return sent
