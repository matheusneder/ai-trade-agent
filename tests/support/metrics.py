"""Métricas em memória: o mesmo ``AgentMetrics`` de produção, lido sob demanda."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from trade_agent import metrics


class Measured:
    def __init__(self, reader: InMemoryMetricReader) -> None:
        self._reader = reader

    def points(self) -> dict[str, list[tuple[dict[str, Any], float]]]:
        """{métrica: [(atributos, valor)]} da coleta atual."""
        found: dict[str, list[tuple[dict[str, Any], float]]] = {}
        data = self._reader.get_metrics_data()
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    found[metric.name] = [
                        (dict(p.attributes or {}), float(p.value))  # type: ignore[union-attr]
                        for p in metric.data.data_points
                    ]
        return found

    def value(self, name: str, **attributes: Any) -> float:
        (value,) = [v for attrs, v in self.points()[name] if attrs == attributes]
        return value

    def resource(self) -> dict[str, Any]:
        data = self._reader.get_metrics_data()
        assert data is not None
        return dict(data.resource_metrics[0].resource.attributes)


@contextmanager
def measuring() -> Iterator[Measured]:
    reader = InMemoryMetricReader()
    agent_metrics = metrics.AgentMetrics(reader, environment="test")
    metrics.install(agent_metrics)
    try:
        yield Measured(reader)
    finally:
        metrics.install(None)
        agent_metrics.shutdown()
