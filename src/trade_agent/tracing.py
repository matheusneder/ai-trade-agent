"""Tracing with OpenTelemetry, exported over OTLP/HTTP to Jaeger.

Each component of the agent is a service in Jaeger (``trade-agent.<component>``, namespace
``trade-agent``). Jaeger links two services when a span of one is the parent of a span of
the other, so the *System Architecture* graph shows who calls whom: the runtime triggers
risk and decision, decision consults the analyst and execution, and all of them reach
Binance and the database.

Every provider shares a single batch processor (one queue and one connection). Without
``TA_OTLP_ENDPOINT``, nothing is installed and spans cost nothing (no-op API).

Secrets never go into spans: only URL paths (without the *query*), method names, counts and
identifiers. Neither does the content of LLM and Telegram messages.
"""

import functools
from collections.abc import Awaitable, Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from enum import Enum
from importlib.metadata import version
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

NAMESPACE = "trade-agent"
COMPONENTS = (
    "runtime",  # background tasks, startup and User Data Stream events
    "risk",  # risk readings and circuit breakers
    "decision",  # decision cycle per profile
    "research",  # news collection and analyst
    "llm",  # Claude API calls
    "execution",  # positions and orders
    "exchange",  # Binance REST
    "reconcile",  # reconciliation with the exchange
    "db",  # PostgreSQL
    "telegram",  # commands and alerts
    "telemetry",  # telemetry snapshots
)
type AttributeValue = str | bool | int | float | Sequence[str]


def service_name(component: str) -> str:
    return f"{NAMESPACE}.{component}"


class Tracing:
    """Providers per component; each processor (one per destination) is shared by all of them."""

    def __init__(self, *processors: SpanProcessor, environment: str) -> None:
        self._processors = processors
        self._providers: dict[str, TracerProvider] = {}
        for component in COMPONENTS:
            resource = Resource.create(
                {
                    "service.name": service_name(component),
                    "service.namespace": NAMESPACE,
                    "service.version": version("trade-agent"),
                    # the current name and the old one (SigNoz groups the metrics by the old one)
                    "deployment.environment.name": environment,
                    "deployment.environment": environment,
                }
            )
            provider = TracerProvider(resource=resource, shutdown_on_exit=False)
            for processor in processors:
                provider.add_span_processor(processor)
            self._providers[component] = provider

    def tracer(self, component: str) -> trace.Tracer:
        return self._providers[component].get_tracer("trade_agent")

    def shutdown(self) -> None:
        """Flushes what is left in the queues and shuts down (once: the processors are shared)."""
        for processor in self._processors:
            processor.shutdown()


_active: Tracing | None = None


def install(tracing: Tracing | None) -> None:
    global _active  # noqa: PLW0603 - a single tracing setup per process
    _active = tracing


def endpoints(value: str | None) -> list[str]:
    """``"http://jaeger:4318, http://signoz-ingester:4318"`` → list, no blanks or trailing ``/``."""
    return [e.strip().rstrip("/") for e in (value or "").split(",") if e.strip()]


def configure_tracing(endpoint: str | None, *, environment: str) -> Tracing | None:
    """Turns on the OTLP/HTTP export to each destination (e.g. ``http://jaeger:4318``).

    Each destination has its own queue: one being down neither delays nor breaks the other.
    """
    targets = endpoints(endpoint)
    if not targets:
        install(None)
        return None
    processors = [
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{target}/v1/traces", timeout=5))
        for target in targets
    ]
    tracing = Tracing(*processors, environment=environment)
    install(tracing)
    return tracing


def tracer(component: str) -> trace.Tracer:
    if _active is None:
        return trace.NoOpTracer()
    return _active.tracer(component)


def _value(value: object) -> AttributeValue | None:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Sequence):
        return [str(v) for v in value]
    return str(value)


def annotate(**attributes: object) -> None:
    """Adds attributes to the current span (``None`` is ignored)."""
    current = trace.get_current_span()
    for key, value in attributes.items():
        converted = _value(value)
        if converted is not None:
            current.set_attribute(f"trade_agent.{key}", converted)


@contextmanager
def span(
    component: str,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Iterator[Span]:
    with tracer(component).start_as_current_span(name, kind=kind, attributes=attributes) as s:
        yield s


def fail(current: Span, exc: BaseException) -> None:
    """Marks the span as an error when the exception is handled (and does not propagate to it)."""
    current.record_exception(exc)
    current.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def traced[**P, R](
    component: str, name: str
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Wraps a coroutine in a span; the body can complete it with :func:`annotate`."""

    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(component, name):
                return await fn(*args, **kwargs)

        return wrapper

    return decorate


def add_trace_ids(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: ``trace_id``/``span_id`` of the current span in every log."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict
