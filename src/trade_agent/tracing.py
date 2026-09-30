"""Rastreamento com OpenTelemetry, exportado por OTLP/HTTP ao Jaeger.

Cada componente do agente é um serviço no Jaeger (``trade-agent.<componente>``, namespace
``trade-agent``). O Jaeger liga dois serviços quando um span de um é pai de um span do
outro, então o grafo *System Architecture* mostra quem chama quem: o runtime dispara o risco
e a decisão, a decisão consulta o analista e a execução, e todos chegam à Binance e ao banco.

Todos os provedores compartilham um único processador em lote (uma fila e uma conexão). Sem
``TA_OTLP_ENDPOINT``, nada é instalado e os spans não custam nada (API no-op).

Segredos nunca vão para os spans: só caminhos de URL (sem *query*), nomes de métodos,
contagens e identificadores. O conteúdo das mensagens do LLM e do Telegram também não.
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
    "runtime",  # tarefas de fundo, partida e eventos do User Data Stream
    "risk",  # leituras de risco e disjuntores
    "decision",  # ciclo de decisão por perfil
    "research",  # coleta de notícias e analista
    "llm",  # chamadas à API Claude
    "execution",  # posições e ordens
    "exchange",  # REST da Binance
    "reconcile",  # reconciliação com a exchange
    "db",  # PostgreSQL
    "telegram",  # comandos e alertas
    "telemetry",  # fotos de telemetria
)
type AttributeValue = str | bool | int | float | Sequence[str]


def service_name(component: str) -> str:
    return f"{NAMESPACE}.{component}"


class Tracing:
    """Provedores por componente com um processador compartilhado."""

    def __init__(self, processor: SpanProcessor, *, environment: str) -> None:
        self._processor = processor
        self._providers: dict[str, TracerProvider] = {}
        for component in COMPONENTS:
            resource = Resource.create(
                {
                    "service.name": service_name(component),
                    "service.namespace": NAMESPACE,
                    "service.version": version("trade-agent"),
                    "deployment.environment.name": environment,
                }
            )
            provider = TracerProvider(resource=resource, shutdown_on_exit=False)
            provider.add_span_processor(processor)
            self._providers[component] = provider

    def tracer(self, component: str) -> trace.Tracer:
        return self._providers[component].get_tracer("trade_agent")

    def shutdown(self) -> None:
        """Envia o que falta na fila e encerra (uma vez: o processador é compartilhado)."""
        self._processor.shutdown()


_active: Tracing | None = None


def install(tracing: Tracing | None) -> None:
    global _active  # noqa: PLW0603 - um único rastreamento por processo
    _active = tracing


def configure_tracing(endpoint: str | None, *, environment: str) -> Tracing | None:
    """Liga a exportação OTLP/HTTP para ``endpoint`` (ex.: ``http://jaeger:4318``)."""
    if endpoint is None:
        install(None)
        return None
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", timeout=5)
    tracing = Tracing(BatchSpanProcessor(exporter), environment=environment)
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
    """Acrescenta atributos ao span atual (``None`` é ignorado)."""
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
    """Marca o span como erro quando a exceção é tratada (e não propaga até ele)."""
    current.record_exception(exc)
    current.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def traced[**P, R](
    component: str, name: str
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Envolve uma corrotina num span; o corpo pode completar com :func:`annotate`."""

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
    """Processador do structlog: ``trace_id``/``span_id`` do span atual em cada log."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict
