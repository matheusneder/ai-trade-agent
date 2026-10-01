"""Gera os dashboards do SigNoz em ``deploy/signoz/dashboards`` e os aplica pela API.

Dashboards como código, no esquema v2 (Perses) do SigNoz 0.144. Edite aqui e rode:

    uv run python -m scripts.signoz_dashboards            # regenera os JSON versionados
    uv run python -m scripts.signoz_dashboards --apply    # cria ou atualiza cada um no SigNoz
    uv run python -m scripts.signoz_dashboards --check    # roda cada consulta (últimas 24 h)

``--apply`` e ``--check`` usam a chave de uma conta de serviço com papel Editor
(``TA_SIGNOZ_API_KEY`` no ``.env``) e ``TA_SIGNOZ_URL`` (padrão ``http://127.0.0.1:8080``).
Os dashboards são identificados pelo ``name`` (``trade-agent-...``): aplicar de novo atualiza.
Um teste garante que os JSON versionados estão em dia com este gerador, que a geometria e os
tipos de consulta seguem as regras do SigNoz e que as métricas usadas existem no agente.
"""

import argparse
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

OUTPUT = Path(__file__).resolve().parents[1] / "deploy" / "signoz" / "dashboards"
SCHEMA_VERSION = "v6"
GRID_COLUMNS = 12
TAGS = [{"key": "projeto", "value": "trade-agent"}]
RED, YELLOW = "#F24769", "#FFCD56"

AGENT_LOGS = "service.name = 'agent'"
JOBS = "service.name = 'trade-agent.runtime' AND name LIKE 'job %'"
EXCHANGE = "service.name = 'trade-agent.exchange'"
DATABASE = "service.name = 'trade-agent.db'"
LLM = "service.name = 'trade-agent.llm'"
PROBLEMS = "severity_text IN ('warn', 'warning', 'error', 'critical', 'fatal')"
ERRORS = "severity_text IN ('error', 'critical', 'fatal')"
CONTAINERS = "service.namespace = 'trade-agent'"

type Json = dict[str, Any]
type Cell = tuple[str, Json, int, int]  # chave do painel, painel, largura, altura


# ================================================================== consultas
def key(name: str, context: str | None = None) -> Json:
    """Campo de agrupamento ou ordenação (``context``: resource, attribute, span, log...)."""
    return {"name": name, **({"fieldContext": context} if context else {})}


def _builder(signal: str, query: str, where: str, by: Sequence[Json], legend: str) -> Json:
    return {
        "name": query,
        "signal": signal,
        "source": "",
        "disabled": False,
        "filter": {"expression": where},
        "groupBy": list(by),
        "order": [],
        "having": {"expression": ""},
        "functions": [],
        "legend": legend,
    }


def metric(
    name: str,
    *,
    time_agg: str = "latest",
    space: str = "avg",
    by: Sequence[Json] = (),
    legend: str = "",
    where: str = "",
    reduce: str | None = None,
    query: str = "A",
) -> Json:
    """Métrica: ``latest``/``avg`` para medidores; ``increase``/``rate`` para contadores."""
    aggregation: Json = {
        "metricName": name,
        "temporality": "",
        "timeAggregation": time_agg,
        "spaceAggregation": space,
    }
    if reduce is not None:  # painéis de valor único reduzem a série a um número
        aggregation["reduceTo"] = reduce
    return {**_builder("metrics", query, where, by, legend), "aggregations": [aggregation]}


def events(
    signal: str,
    expression: str,
    *,
    where: str,
    by: Sequence[Json] = (),
    legend: str = "",
    query: str = "A",
    limit: int | None = None,
) -> Json:
    """Logs ou traces agregados (``count()``, ``p95(duration_nano)``...)."""
    spec = {
        **_builder(signal, query, where, by, legend),
        "aggregations": [{"expression": expression}],
    }
    if limit is not None:  # séries mais altas primeiro
        spec["order"] = [{"key": {"name": expression}, "direction": "desc"}]
        spec["limit"] = limit
    return spec


def rows(signal: str, *, where: str, limit: int = 50) -> Json:
    """Registros brutos (logs ou spans) mais recentes, para os painéis de lista."""
    return {
        **_builder(signal, "A", where, (), ""),
        "order": [{"key": key("timestamp"), "direction": "desc"}],
        "limit": limit,
    }


# ================================================================== painéis
def _panel(
    title: str,
    kind: str,
    spec: Json,
    queries: Sequence[Json],
    *,
    request: str,
    description: str,
) -> Json:
    if len(queries) == 1:
        plugin: Json = {"kind": "signoz/BuilderQuery", "spec": queries[0]}
    else:
        composite = [{"type": "builder_query", "spec": q} for q in queries]
        plugin = {"kind": "signoz/CompositeQuery", "spec": {"queries": composite}}
    return {
        "kind": "Panel",
        "spec": {
            "display": {"name": title, "description": description},
            "plugin": {"kind": kind, "spec": spec},
            "queries": [{"kind": request, "spec": {"plugin": plugin}}],
            "links": [],
        },
    }


def _formatting(unit: str, decimals: int) -> Json:
    return {"unit": unit, "decimalPrecision": str(decimals)}


def series(
    title: str,
    *queries: Json,
    unit: str = "none",
    decimals: int = 2,
    steps: bool = False,
    description: str = "",
) -> Json:
    spec: Json = {
        "visualization": {"timePreference": "global_time"},
        "formatting": _formatting(unit, decimals),
        "legend": {"position": "bottom"},
    }
    if steps:  # estados e contagens mudam em degraus, não em curvas
        spec["chartAppearance"] = {"lineInterpolation": "step_after"}
    return _panel(
        title,
        "signoz/TimeSeriesPanel",
        spec,
        queries,
        request="time_series",
        description=description,
    )


def bars(
    title: str, *queries: Json, unit: str = "none", decimals: int = 0, description: str = ""
) -> Json:
    spec = {
        "visualization": {"timePreference": "global_time", "stackedBarChart": True},
        "formatting": _formatting(unit, decimals),
        "legend": {"position": "bottom"},
    }
    return _panel(
        title, "signoz/BarChartPanel", spec, queries, request="time_series", description=description
    )


def number(
    title: str,
    query: Json,
    *,
    unit: str = "none",
    decimals: int = 2,
    warn: float | None = None,
    alert: float | None = None,
    description: str = "",
) -> Json:
    """Valor único do período; ``warn``/``alert``: a partir de quanto fica amarelo/vermelho."""
    thresholds = [
        {"value": value, "operator": "above_or_equal", "color": color, "format": "text"}
        for value, color in ((warn, YELLOW), (alert, RED))
        if value is not None
    ]
    spec = {
        "visualization": {"timePreference": "global_time"},
        "formatting": _formatting(unit, decimals),
        "thresholds": thresholds,
    }
    return _panel(
        title, "signoz/NumberPanel", spec, [query], request="scalar", description=description
    )


def listing(title: str, query: Json, fields: Sequence[Json], *, description: str = "") -> Json:
    spec = {"selectFields": list(fields)}
    return _panel(title, "signoz/ListPanel", spec, [query], request="raw", description=description)


# ================================================================== dashboards
def cell(panel_key: str, panel: Json, width: int, height: int = 6) -> Cell:
    return panel_key, panel, width, height


def dashboard(
    name: str,
    title: str,
    description: str,
    sections: Sequence[tuple[str, Sequence[Sequence[Cell]]]],
) -> Json:
    """Uma grade por seção (com título, recolhível); cada linha é preenchida da esquerda."""
    panels: Json = {}
    layouts: list[Json] = []
    for section, lines in sections:
        items: list[Json] = []
        y = 0
        for line in lines:
            x = 0
            for panel_key, panel, width, height in line:
                panels[panel_key] = panel
                ref = {"$ref": f"#/spec/panels/{panel_key}"}
                items.append({"x": x, "y": y, "width": width, "height": height, "content": ref})
                x += width
            y += max(height for _, _, _, height in line)
        display = {"title": section, "collapse": {"open": True}}
        layouts.append({"kind": "Grid", "spec": {"display": display, "items": items}})
    return {
        "schemaVersion": SCHEMA_VERSION,
        "image": "",
        "name": name,
        "generateName": False,
        "tags": TAGS,
        "spec": {
            "display": {"name": title, "description": description},
            "variables": [],
            "panels": panels,
            "layouts": layouts,
            "duration": "6h",
            "refreshInterval": "1m",
        },
    }


def _operation() -> Json:
    profile, scope = key("profile", "attribute"), key("scope", "attribute")
    return dashboard(
        "trade-agent-operacao",
        "Trade Agent · Operação",
        "Patrimônio, resultado, risco e decisões do agente (métricas a cada minuto).",
        [
            (
                "Agora",
                [
                    [
                        cell(
                            "equity-now",
                            number(
                                "Patrimônio (USDT)", metric("trade_agent.equity", reduce="last")
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "pnl-now",
                            number(
                                "Resultado do dia (USDT)",
                                metric("trade_agent.pnl.daily", reduce="last"),
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "drawdown-now",
                            number(
                                "Drawdown",
                                metric("trade_agent.drawdown", reduce="last"),
                                unit="percent",
                                warn=5,
                                alert=10,
                                description="Queda desde o pico do patrimônio.",
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "exposure-now",
                            number(
                                "Exposição (USDT)", metric("trade_agent.exposure", reduce="last")
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "positions-now",
                            number(
                                "Posições ativas",
                                metric("trade_agent.positions.active", reduce="last"),
                                decimals=0,
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "risk-now",
                            number(
                                "Pior estado do risco",
                                metric("trade_agent.risk.state", space="max", reduce="last"),
                                decimals=0,
                                warn=1,
                                alert=2,
                                description="0 operando · 1 pausado · 2 parado · 3 vendendo tudo.",
                            ),
                            2,
                            3,
                        ),
                    ]
                ],
            ),
            (
                "Patrimônio e resultado",
                [
                    [
                        cell(
                            "equity",
                            series(
                                "Patrimônio, abertura do dia e pico (USDT)",
                                metric("trade_agent.equity", legend="patrimônio"),
                                metric(
                                    "trade_agent.equity.day_start",
                                    legend="abertura do dia",
                                    query="B",
                                ),
                                metric("trade_agent.equity.peak", legend="pico", query="C"),
                            ),
                            6,
                        ),
                        cell(
                            "pnl",
                            series(
                                "Resultado (USDT)",
                                metric("trade_agent.pnl.daily", legend="do dia"),
                                metric("trade_agent.pnl.realized", legend="realizado", query="B"),
                                metric("trade_agent.pnl.unrealized", legend="aberto", query="C"),
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "drawdown",
                            series("Drawdown", metric("trade_agent.drawdown"), unit="percent"),
                            4,
                        ),
                        cell(
                            "exposure",
                            series("Exposição (USDT)", metric("trade_agent.exposure")),
                            4,
                        ),
                        cell(
                            "positions",
                            series(
                                "Posições ativas",
                                metric("trade_agent.positions.active"),
                                decimals=0,
                                steps=True,
                            ),
                            4,
                        ),
                    ],
                ],
            ),
            (
                "Risco e decisões",
                [
                    [
                        cell(
                            "risk-state",
                            series(
                                "Estado do risco por escopo",
                                metric(
                                    "trade_agent.risk.state",
                                    space="max",
                                    by=[scope],
                                    legend="{{scope}}",
                                ),
                                decimals=0,
                                steps=True,
                                description="0 operando · 1 pausado · 2 parado · 3 vendendo tudo.",
                            ),
                            6,
                        ),
                        cell(
                            "state-changes",
                            bars(
                                "Mudanças de estado",
                                metric(
                                    "trade_agent.risk.state_changes",
                                    time_agg="increase",
                                    space="sum",
                                    by=[scope, key("state", "attribute")],
                                    legend="{{scope}} → {{state}}",
                                ),
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "cycles",
                            bars(
                                "Ciclos de decisão por perfil",
                                metric(
                                    "trade_agent.decision.cycles",
                                    time_agg="increase",
                                    space="sum",
                                    by=[profile],
                                    legend="{{profile}}",
                                ),
                            ),
                            6,
                        ),
                        cell(
                            "entries-exits",
                            bars(
                                "Entradas e saídas por perfil",
                                metric(
                                    "trade_agent.decision.entries",
                                    time_agg="increase",
                                    space="sum",
                                    by=[profile],
                                    legend="entradas {{profile}}",
                                ),
                                metric(
                                    "trade_agent.decision.exits",
                                    time_agg="increase",
                                    space="sum",
                                    by=[profile],
                                    legend="saídas {{profile}}",
                                    query="B",
                                ),
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "last-cycles",
                            listing(
                                "Últimos ciclos de decisão",
                                rows("logs", where=f"{AGENT_LOGS} AND event = 'decision.cycle'"),
                                [
                                    key("profile", "attribute"),
                                    key("state", "attribute"),
                                    key("evaluated", "attribute"),
                                    key("research", "attribute"),
                                ],
                            ),
                            12,
                            7,
                        )
                    ],
                ],
            ),
        ],
    )


def _health() -> Json:
    span_name = key("name", "span")
    return dashboard(
        "trade-agent-saude",
        "Trade Agent · Saúde técnica",
        "Binance, relógio, tarefas de fundo, banco e logs de erro.",
        [
            (
                "Agora",
                [
                    [
                        cell(
                            "errors-now",
                            number(
                                "Falhas na Binance (5 min)",
                                metric("trade_agent.binance.error_rate", reduce="last"),
                                unit="percentunit",
                                warn=0.05,
                                alert=0.2,
                                description="Sem conexão, resultado desconhecido, 5xx, 418 e 429.",
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "weight-now",
                            number(
                                "Peso usado (1 min)",
                                metric("trade_agent.binance.weight_used_1m", reduce="last"),
                                decimals=0,
                                warn=3000,
                                alert=5000,
                                description="Limite da Binance: 6000 por minuto.",
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "clock-now",
                            number(
                                "Desvio do relógio",
                                metric("trade_agent.clock.offset", reduce="last"),
                                unit="ms",
                                decimals=0,
                                warn=500,
                                alert=1000,
                                description="Horário da Binance menos o relógio local.",
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "log-errors-now",
                            number(
                                "Erros nos logs (período)",
                                events("logs", "count()", where=ERRORS),
                                decimals=0,
                                warn=1,
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "job-failures-now",
                            number(
                                "Tarefas com falha (período)",
                                events("traces", "count()", where=f"{JOBS} AND has_error = true"),
                                decimals=0,
                                warn=1,
                            ),
                            2,
                            3,
                        ),
                        cell(
                            "requests-now",
                            number(
                                "Requisições à Binance (período)",
                                events("traces", "count()", where=EXCHANGE),
                                decimals=0,
                            ),
                            2,
                            3,
                        ),
                    ]
                ],
            ),
            (
                "Binance e relógio",
                [
                    [
                        cell(
                            "errors",
                            series(
                                "Falhas na Binance (janela de 5 min)",
                                metric("trade_agent.binance.error_rate"),
                                unit="percentunit",
                            ),
                            4,
                        ),
                        cell(
                            "weight",
                            series(
                                "Peso usado por minuto",
                                metric("trade_agent.binance.weight_used_1m"),
                                decimals=0,
                            ),
                            4,
                        ),
                        cell(
                            "clock",
                            series(
                                "Desvio do relógio",
                                metric("trade_agent.clock.offset"),
                                unit="ms",
                                decimals=0,
                            ),
                            4,
                        ),
                    ],
                    [
                        cell(
                            "endpoint-latency",
                            series(
                                "Latência p95 por endpoint da Binance",
                                events(
                                    "traces",
                                    "p95(duration_nano)",
                                    where=EXCHANGE,
                                    by=[span_name],
                                    legend="{{name}}",
                                    limit=10,
                                ),
                                unit="ns",
                            ),
                            6,
                        ),
                        cell(
                            "http-errors",
                            bars(
                                "Respostas de erro da Binance por status",
                                events(
                                    "traces",
                                    "count()",
                                    where=f"{EXCHANGE} AND http.response.status_code >= 400",
                                    by=[key("http.response.status_code", "attribute")],
                                    legend="HTTP {{http.response.status_code}}",
                                ),
                            ),
                            6,
                        ),
                    ],
                ],
            ),
            (
                "Tarefas e banco",
                [
                    [
                        cell(
                            "job-latency",
                            series(
                                "Duração p95 das tarefas",
                                events(
                                    "traces",
                                    "p95(duration_nano)",
                                    where=JOBS,
                                    by=[span_name],
                                    legend="{{name}}",
                                ),
                                unit="ns",
                            ),
                            6,
                        ),
                        cell(
                            "job-failures",
                            bars(
                                "Falhas por tarefa",
                                events(
                                    "traces",
                                    "count()",
                                    where=f"{JOBS} AND has_error = true",
                                    by=[span_name],
                                    legend="{{name}}",
                                ),
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "db-latency",
                            series(
                                "Latência p95 do banco por operação",
                                events(
                                    "traces",
                                    "p95(duration_nano)",
                                    where=DATABASE,
                                    by=[span_name],
                                    legend="{{name}}",
                                    limit=10,
                                ),
                                unit="ns",
                            ),
                            6,
                        ),
                        cell(
                            "spans",
                            bars(
                                "Spans por componente",
                                events(
                                    "traces",
                                    "count()",
                                    where="service.name LIKE 'trade-agent.%'",
                                    by=[key("service.name", "resource")],
                                    legend="{{service.name}}",
                                ),
                            ),
                            6,
                        ),
                    ],
                ],
            ),
            (
                "Logs",
                [
                    [
                        cell(
                            "problems",
                            bars(
                                "Avisos e erros por serviço",
                                events(
                                    "logs",
                                    "count()",
                                    where=PROBLEMS,
                                    by=[
                                        key("service.name", "resource"),
                                        key("severity_text", "log"),
                                    ],
                                    legend="{{service.name}} {{severity_text}}",
                                ),
                            ),
                            12,
                        )
                    ],
                    [
                        cell(
                            "agent-problems",
                            listing(
                                "Últimos avisos e erros do agente",
                                rows("logs", where=f"{AGENT_LOGS} AND {PROBLEMS}"),
                                [key("event", "attribute"), key("error", "attribute")],
                            ),
                            12,
                            7,
                        )
                    ],
                ],
            ),
        ],
    )


def _llm() -> Json:
    model, purpose = key("model", "attribute"), key("purpose", "attribute")
    return dashboard(
        "trade-agent-llm",
        "Trade Agent · LLM",
        "Custo, tokens e latência do analista de mercado. O orçamento diário fica em "
        "config/research.yaml (budget.daily_usd).",
        [
            (
                "Período",
                [
                    [
                        cell(
                            "cost-now",
                            number(
                                "Custo no período (US$)",
                                metric(
                                    "trade_agent.llm.cost",
                                    time_agg="increase",
                                    space="sum",
                                    reduce="sum",
                                ),
                                decimals=4,
                            ),
                            3,
                            3,
                        ),
                        cell(
                            "tokens-now",
                            number(
                                "Tokens no período",
                                metric(
                                    "trade_agent.llm.tokens",
                                    time_agg="increase",
                                    space="sum",
                                    reduce="sum",
                                ),
                                decimals=0,
                            ),
                            3,
                            3,
                        ),
                        cell(
                            "calls-now",
                            number(
                                "Chamadas no período",
                                events("traces", "count()", where=LLM),
                                decimals=0,
                            ),
                            3,
                            3,
                        ),
                        cell(
                            "latency-now",
                            number(
                                "Latência p95 das chamadas",
                                events("traces", "p95(duration_nano)", where=LLM),
                                unit="ns",
                            ),
                            3,
                            3,
                        ),
                    ]
                ],
            ),
            (
                "Custo e uso",
                [
                    [
                        cell(
                            "cost",
                            bars(
                                "Custo por modelo e finalidade (US$)",
                                metric(
                                    "trade_agent.llm.cost",
                                    time_agg="increase",
                                    space="sum",
                                    by=[model, purpose],
                                    legend="{{model}} · {{purpose}}",
                                ),
                                decimals=4,
                            ),
                            6,
                        ),
                        cell(
                            "tokens",
                            bars(
                                "Tokens por direção",
                                metric(
                                    "trade_agent.llm.tokens",
                                    time_agg="increase",
                                    space="sum",
                                    by=[key("direction", "attribute")],
                                    legend="{{direction}}",
                                ),
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "calls",
                            bars(
                                "Chamadas por modelo e finalidade",
                                events(
                                    "traces",
                                    "count()",
                                    where=LLM,
                                    by=[
                                        key("gen_ai.request.model", "attribute"),
                                        key("trade_agent.purpose", "attribute"),
                                    ],
                                    legend="{{gen_ai.request.model}} · {{trade_agent.purpose}}",
                                ),
                            ),
                            6,
                        ),
                        cell(
                            "latency",
                            series(
                                "Latência das chamadas",
                                events("traces", "p50(duration_nano)", where=LLM, legend="p50"),
                                events(
                                    "traces",
                                    "p95(duration_nano)",
                                    where=LLM,
                                    legend="p95",
                                    query="B",
                                ),
                                unit="ns",
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "llm-calls",
                            listing(
                                "Últimas chamadas ao LLM",
                                rows("traces", where=LLM, limit=30),
                                [
                                    key("name", "span"),
                                    key("duration_nano", "span"),
                                    key("trade_agent.purpose", "attribute"),
                                    key("gen_ai.usage.input_tokens", "attribute"),
                                    key("gen_ai.usage.output_tokens", "attribute"),
                                    key("trade_agent.cost_usd", "attribute"),
                                ],
                            ),
                            12,
                            7,
                        )
                    ],
                ],
            ),
        ],
    )


def _containers() -> Json:
    service = key("compose.service")

    def per_service(name: str, *, time_agg: str = "latest") -> Json:
        """Uma série por serviço do compose; contadores (``rate``) somam as interfaces."""
        space = "sum" if time_agg == "rate" else "avg"
        legend = "{{compose.service}}"
        return metric(
            name, time_agg=time_agg, space=space, by=[service], legend=legend, where=CONTAINERS
        )

    return dashboard(
        "trade-agent-conteineres",
        "Trade Agent · Contêineres",
        "CPU, memória, rede e disco de cada contêiner do projeto (docker_stats, a cada 30 s).",
        [
            (
                "Agora",
                [
                    [
                        cell(
                            "cpu-now",
                            number(
                                "CPU de todos os contêineres",
                                metric(
                                    "container.cpu.utilization",
                                    space="sum",
                                    where=CONTAINERS,
                                    reduce="last",
                                ),
                                unit="percent",
                                decimals=1,
                            ),
                            4,
                            3,
                        ),
                        cell(
                            "memory-now",
                            number(
                                "Memória de todos os contêineres",
                                metric(
                                    "container.memory.usage.total",
                                    space="sum",
                                    where=CONTAINERS,
                                    reduce="last",
                                ),
                                unit="bytes",
                                decimals=1,
                            ),
                            4,
                            3,
                        ),
                        cell(
                            "containers-now",
                            number(
                                "Contêineres medidos",
                                metric(
                                    "container.memory.usage.total",
                                    space="count",
                                    where=CONTAINERS,
                                    reduce="last",
                                ),
                                decimals=0,
                            ),
                            4,
                            3,
                        ),
                    ]
                ],
            ),
            (
                "Uso por serviço",
                [
                    [
                        cell(
                            "cpu",
                            series(
                                "CPU por serviço",
                                per_service("container.cpu.utilization"),
                                unit="percent",
                            ),
                            6,
                        ),
                        cell(
                            "memory",
                            series(
                                "Memória por serviço",
                                per_service("container.memory.usage.total"),
                                unit="bytes",
                            ),
                            6,
                        ),
                    ],
                    [
                        cell(
                            "rx",
                            series(
                                "Rede recebida por serviço",
                                per_service("container.network.io.usage.rx_bytes", time_agg="rate"),
                                unit="binBps",
                            ),
                            4,
                        ),
                        cell(
                            "tx",
                            series(
                                "Rede enviada por serviço",
                                per_service("container.network.io.usage.tx_bytes", time_agg="rate"),
                                unit="binBps",
                            ),
                            4,
                        ),
                        cell(
                            "disk",
                            series(
                                "Disco (leitura + escrita) por serviço",
                                metric(
                                    "container.blockio.io_service_bytes_recursive",
                                    time_agg="rate",
                                    space="sum",
                                    by=[service],
                                    legend="{{compose.service}}",
                                    where=f"{CONTAINERS} AND operation IN ('read', 'write')",
                                ),
                                unit="binBps",
                            ),
                            4,
                        ),
                    ],
                    [
                        cell(
                            "memory-limit",
                            series(
                                "Memória em relação ao limite",
                                per_service("container.memory.percent"),
                                unit="percent",
                            ),
                            6,
                        ),
                        cell(
                            "log-volume",
                            bars(
                                "Linhas de log por serviço",
                                events(
                                    "logs",
                                    "count()",
                                    where="service.namespace = 'trade-agent'",
                                    by=[key("service.name", "resource")],
                                    legend="{{service.name}}",
                                ),
                            ),
                            6,
                        ),
                    ],
                ],
            ),
        ],
    )


def build() -> dict[str, Json]:
    boards = [_operation(), _health(), _llm(), _containers()]
    return {f"{board['name']}.json": board for board in boards}


def render(board: Json) -> str:
    return json.dumps(board, ensure_ascii=False, indent=2) + "\n"


# ================================================================== API do SigNoz
class SigNozSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TA_SIGNOZ_", env_file=".env", extra="ignore")
    api_key: SecretStr
    url: str = "http://127.0.0.1:8080"


def client(settings: SigNozSettings) -> httpx.Client:
    headers = {"SIGNOZ-API-KEY": settings.api_key.get_secret_value()}
    return httpx.Client(base_url=settings.url, headers=headers, timeout=30)


def _data(response: httpx.Response) -> Any:
    if response.is_error:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: {response.text}"
        )
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


def existing(http: httpx.Client) -> dict[str, str]:
    """``name`` → ``id`` dos dashboards já criados."""
    data = _data(http.get("/api/v2/dashboards", params={"limit": 200}))
    listed = data.get("dashboards", []) if isinstance(data, dict) else data
    return {board["name"]: board["id"] for board in listed}


def apply(http: httpx.Client, boards: dict[str, Json]) -> None:
    ids = existing(http)
    for board in boards.values():
        name = board["name"]
        if name in ids:  # a atualização não aceita generateName
            body = {k: v for k, v in board.items() if k != "generateName"}
            _data(http.put(f"/api/v2/dashboards/{ids[name]}", json=body))
            print(f"atualizado: {name}")
        else:
            created = _data(http.post("/api/v2/dashboards", json=board))
            print(f"criado: {name} ({created.get('id', '?')})")


def _queries(panel: Json) -> list[tuple[str, Json]]:
    (query,) = panel["spec"]["queries"]
    plugin = query["spec"]["plugin"]
    if plugin["kind"] == "signoz/CompositeQuery":
        envelopes = plugin["spec"]["queries"]
    else:
        envelopes = [{"type": "builder_query", "spec": plugin["spec"]}]
    return [(query["kind"], {"queries": envelopes})]


def _summary(data: Any) -> str:
    """Quantas séries, linhas ou valores a consulta devolveu."""
    results = (data.get("data") or {}).get("results") or []
    counts = []
    for result in results:
        if "aggregations" in result:  # série temporal
            counts.append(sum(len(a.get("series") or []) for a in result["aggregations"] or []))
        elif "rows" in result:  # lista
            counts.append(len(result["rows"] or []))
        elif "data" in result:  # escalar
            counts.append(len(result["data"] or []))
    return "+".join(map(str, counts)) or "0"


def check(http: httpx.Client, boards: dict[str, Json], *, hours: float = 24) -> int:
    """Roda cada consulta dos painéis na janela recente; devolve quantas falharam ou vieram
    vazias (vazio pode ser só falta de eventos: sem mudança de estado, sem falha)."""
    end = int(time.time() * 1000)
    start = end - int(hours * 3_600_000)
    empty = 0
    for board in boards.values():
        print(f"== {board['spec']['display']['name']}")
        for panel in board["spec"]["panels"].values():
            title = panel["spec"]["display"]["name"]
            for request, composite in _queries(panel):
                body = {
                    "schemaVersion": "v1",
                    "start": start,
                    "end": end,
                    "requestType": request,
                    "compositeQuery": composite,
                }
                try:
                    found = _summary(_data(http.post("/api/v5/query_range", json=body)))
                except RuntimeError as exc:
                    found = f"ERRO {exc}"
                if found.startswith("ERRO") or found.strip("0+") == "":
                    empty += 1
                print(f"  {found:>8}  {title}")
    return empty


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="cria ou atualiza no SigNoz")
    parser.add_argument("--check", action="store_true", help="roda cada consulta (24 h)")
    args = parser.parse_args(argv)
    boards = build()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, board in boards.items():
        (OUTPUT / name).write_text(render(board), encoding="utf-8", newline="\n")
        print(OUTPUT / name)
    if args.apply or args.check:
        with client(SigNozSettings()) as http:  # a chave vem do .env
            if args.apply:
                apply(http, boards)
            if args.check:
                print(f"consultas com erro ou sem dados: {check(http, boards)}")


if __name__ == "__main__":
    main()
