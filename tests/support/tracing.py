"""Rastreamento em memória: os mesmos provedores por componente, sem exportar nada."""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from trade_agent import tracing


def component(span: ReadableSpan) -> str:
    return str(span.resource.attributes["service.name"]).removeprefix(f"{tracing.NAMESPACE}.")


class Recorded:
    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    @property
    def spans(self) -> list[ReadableSpan]:
        return list(self._exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [s for s in self.spans if s.name == name]

    def one(self, name: str) -> ReadableSpan:
        (found,) = self.named(name)
        return found

    def parent(self, span: ReadableSpan) -> ReadableSpan | None:
        if span.parent is None:
            return None
        return next((s for s in self.spans if s.context.span_id == span.parent.span_id), None)

    def edges(self) -> set[tuple[str, str]]:
        """Pares (componente pai → filho) entre componentes diferentes, como o Jaeger calcula."""
        pairs: set[tuple[str, str]] = set()
        for span in self.spans:
            parent = self.parent(span)
            if parent is not None and component(parent) != component(span):
                pairs.add((component(parent), component(span)))
        return pairs

    def attribute_text(self) -> str:
        """Todos os valores de atributos e eventos, para procurar segredos."""
        parts: list[str] = []
        for span in self.spans:
            parts.append(span.name)
            parts.extend(str(v) for v in (span.attributes or {}).values())
            for event in span.events:
                parts.extend(str(v) for v in (event.attributes or {}).values())
        return "\n".join(parts)

    def clear(self) -> None:
        self._exporter.clear()


@contextmanager
def recording() -> Iterator[Recorded]:
    exporter = InMemorySpanExporter()
    traces = tracing.Tracing(SimpleSpanProcessor(exporter), environment="test")
    tracing.install(traces)
    try:
        yield Recorded(exporter)
    finally:
        tracing.install(None)
        traces.shutdown()
