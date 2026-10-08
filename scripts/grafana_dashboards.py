"""Generates the Grafana dashboards (doc 03, §12.1) in ``deploy/grafana/dashboards``.

Dashboards as code: edit here and run ``uv run python -m scripts.grafana_dashboards``.
A test makes sure the versioned JSON files are in sync with this generator and that every
query runs: SQL on the database schema and LogQL on a Loki with the production configuration.
"""

import json
from pathlib import Path
from typing import Any

OUTPUT = Path(__file__).resolve().parents[1] / "deploy" / "grafana" / "dashboards"
DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "trade-agent-pg"}
LOKI = {"type": "loki", "uid": "trade-agent-loki"}
CLOSED = "state = 'closed' AND realized_pnl IS NOT NULL"
LATEST = "id = (SELECT max(id) FROM telemetry_snapshots)"
PROBLEMS = 'level=~"warning|warn|error|critical|fatal"'
FILTERED = '{service=~"$service", level=~"$level"} |~ "(?i)$busca"'


def panel(
    title: str,
    sql: str,
    *,
    kind: str = "timeseries",
    fmt: str = "time_series",
    unit: str | None = None,
    w: int = 12,
    h: int = 8,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit} if unit else {}
    return {
        "type": kind,
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": {"w": w, "h": h},
        "targets": [
            {
                "refId": "A",
                "datasource": DATASOURCE,
                "editorMode": "code",
                "rawQuery": True,
                "format": fmt,
                "rawSql": " ".join(sql.split()),
            }
        ],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }


def stat(title: str, sql: str, unit: str | None = None, w: int = 6) -> dict[str, Any]:
    return panel(title, sql, kind="stat", fmt="table", unit=unit, w=w, h=4)


def table(title: str, sql: str, w: int = 24, h: int = 9) -> dict[str, Any]:
    return panel(title, sql, kind="table", fmt="table", w=w, h=h)


def logql(
    title: str,
    expr: str,
    *,
    kind: str = "timeseries",
    instant: bool = False,
    legend: str | None = None,
    w: int = 12,
    h: int = 8,
) -> dict[str, Any]:
    """Panel over Loki: time series, ``stat``/``table`` (instant query) or ``logs``."""
    target: dict[str, Any] = {
        "refId": "A",
        "datasource": LOKI,
        "editorMode": "code",
        "expr": expr,
        "queryType": "instant" if instant else "range",
    }
    if legend:
        target["legendFormat"] = legend
    options: dict[str, Any] = {}
    if kind == "logs":
        options = {
            "showTime": True,
            "wrapLogMessage": True,
            "enableLogDetails": True,
            "sortOrder": "Descending",
            "dedupStrategy": "none",
        }
    return {
        "type": kind,
        "title": title,
        "datasource": LOKI,
        "gridPos": {"w": w, "h": h},
        "targets": [target],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": options,
    }


def label_variable(name: str, label: str, all_value: str) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": LOKI,
        "query": f"label_values({name})",
        "refresh": 2,  # when the period changes
        "multi": True,
        "includeAll": True,
        "allValue": all_value,
        "current": {"text": "All", "value": "$__all"},
        "sort": 1,
    }


def dashboard(
    uid: str,
    title: str,
    panels: list[dict[str, Any]],
    *,
    since: str = "now-7d",
    variables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    x = y = row_h = 0
    for index, p in enumerate(panels, start=1):
        grid = p["gridPos"]
        if x + grid["w"] > 24:
            x, y, row_h = 0, y + row_h, 0
        grid.update({"x": x, "y": y})
        x += grid["w"]
        row_h = max(row_h, grid["h"])
        p["id"] = index
    board: dict[str, Any] = {
        "uid": uid,
        "title": f"Trade Agent — {title}",
        "tags": ["trade-agent"],
        "timezone": "utc",
        "editable": True,
        "refresh": "1m",
        "schemaVersion": 41,
        "version": 1,
        "time": {"from": since, "to": "now"},
        "panels": panels,
    }
    if variables:
        board["templating"] = {"list": variables}
    return board


def build() -> dict[str, dict[str, Any]]:
    overview = dashboard("ta-overview", "Visão geral", [
        stat("Patrimônio", f"SELECT equity::float AS patrimonio FROM telemetry_snapshots WHERE {LATEST}", "currencyUSD"),
        stat("PnL do dia", f"SELECT (equity - day_start_equity)::float AS pnl_dia FROM telemetry_snapshots WHERE {LATEST}", "currencyUSD"),
        stat("Drawdown", f"SELECT drawdown_pct AS drawdown FROM telemetry_snapshots WHERE {LATEST}", "percent"),
        stat("Posições ativas", f"SELECT active_positions AS posicoes, exposure::float AS exposicao FROM telemetry_snapshots WHERE {LATEST}"),
        panel("Patrimônio e pico", "SELECT at AS time, equity::float AS patrimonio, peak_equity::float AS pico FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", unit="currencyUSD"),
        panel("Drawdown (%)", "SELECT at AS time, drawdown_pct AS drawdown FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", unit="percent"),
        table("Estado operacional", f"SELECT key AS escopo, value AS estado FROM telemetry_snapshots, jsonb_each_text(states) WHERE {LATEST} ORDER BY 1", w=8, h=8),
        table("Mudanças de estado (risco)", "SELECT created_at AS time, payload->>'scope' AS escopo, payload->>'state' AS estado, payload->>'reason' AS motivo, payload->>'source' AS origem FROM events WHERE kind = 'risk.state_changed' AND $__timeFilter(created_at) ORDER BY created_at DESC LIMIT 50", w=16, h=8),
        table("Última execução por tarefa", "SELECT kind AS tarefa, max(created_at) AS ultima FROM events WHERE kind IN ('agent.started', 'decision.cycle', 'risk.state_changed') GROUP BY kind ORDER BY 2 DESC", h=6),
    ])  # fmt: skip
    positions = dashboard("ta-positions", "Posições", [
        table("Posições ativas", "SELECT symbol AS ativo, profile AS perfil, state AS estado, entry_price::float AS entrada, protected_qty::float AS quantidade, (policy->>'stop_pct')::float AS stop_pct, (policy->>'take_profit_pct')::float AS tp_pct, (policy->>'take_profit_trailing_bips')::int AS trailing_bips, opened_at AS aberta_em, now() - opened_at AS tempo FROM positions WHERE state NOT IN ('closed', 'rejected') ORDER BY opened_at"),
        panel("PnL não realizado", "SELECT at AS time, unrealized_pnl::float AS nao_realizado FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", unit="currencyUSD"),
        panel("Exposição", "SELECT at AS time, exposure::float AS exposicao FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", unit="currencyUSD"),
        table("Encerradas no período", f"SELECT closed_at AS time, symbol AS ativo, profile AS perfil, exit_reason AS motivo, entry_price::float AS entrada, exit_price::float AS saida, realized_pnl::float AS pnl FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at) ORDER BY closed_at DESC"),
    ])  # fmt: skip
    performance = dashboard("ta-performance", "Performance", [
        panel("PnL realizado acumulado", f"SELECT closed_at AS time, sum(realized_pnl) OVER (ORDER BY closed_at, id)::float AS pnl_acumulado FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at) ORDER BY 1", unit="currencyUSD", w=24),
        stat("Trades", f"SELECT count(*) AS trades FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at)"),
        stat("Taxa de acerto", f"SELECT coalesce(avg((realized_pnl > 0)::int) * 100, 0)::float AS acerto FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at)", "percent"),
        stat("Profit factor", f"SELECT (sum(realized_pnl) FILTER (WHERE realized_pnl > 0) / nullif(-sum(realized_pnl) FILTER (WHERE realized_pnl < 0), 0))::float AS profit_factor FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at)"),
        stat("Taxas pagas (USDT)", f"SELECT coalesce(sum((fees->>'USDT')::numeric), 0)::float AS taxas FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at)", "currencyUSD"),
        table("Por perfil e ativo", f"SELECT profile AS perfil, symbol AS ativo, count(*) AS trades, sum(realized_pnl)::float AS pnl, (avg((realized_pnl > 0)::int) * 100)::float AS acerto FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at) GROUP BY 1, 2 ORDER BY 4 DESC", w=16),
        table("Motivos de saída", f"SELECT exit_reason AS motivo, count(*) AS trades, sum(realized_pnl)::float AS pnl FROM positions WHERE {CLOSED} AND $__timeFilter(closed_at) GROUP BY 1 ORDER BY 2 DESC", w=8),
    ])  # fmt: skip
    research = dashboard("ta-research", "Decisões e pesquisa", [
        table("Ciclos de decisão", "SELECT created_at AS time, payload->>'profile' AS perfil, payload->>'state' AS estado, (payload->>'dry_run')::bool AS simulacao, payload->>'research' AS analista, payload->>'opened' AS entradas, payload->>'exits' AS saidas, payload->>'rejected' AS recusas FROM events WHERE kind = 'decision.cycle' AND $__timeFilter(created_at) ORDER BY created_at DESC LIMIT 200"),
        table("Última leitura do analista", "SELECT a->>'asset' AS ativo, (a->>'sentiment')::float AS sentimento, (a->>'confidence')::float AS confianca, (a->>'veto')::bool AS veto, a->>'rationale' AS justificativa FROM research_reports r, jsonb_array_elements(r.view->'assets') a WHERE r.id = (SELECT max(id) FROM research_reports WHERE status = 'ok')", w=16),
        stat("Regime e exposição", "SELECT view->>'market_regime' AS regime, (view->>'exposure_multiplier')::float AS exposicao FROM research_reports WHERE id = (SELECT max(id) FROM research_reports WHERE status = 'ok')", w=8),
        panel("Custo diário do LLM", "SELECT date_trunc('day', at) AS time, sum(cost_usd)::float AS custo FROM llm_usage WHERE $__timeFilter(at) GROUP BY 1 ORDER BY 1", kind="barchart", unit="currencyUSD"),
        panel("Ciclos do analista por status", "SELECT date_trunc('day', created_at) AS time, count(*) FILTER (WHERE status = 'ok') AS ok, count(*) FILTER (WHERE status = 'failed') AS falhas FROM research_reports WHERE $__timeFilter(created_at) GROUP BY 1 ORDER BY 1", kind="barchart"),
        table("Notícias coletadas por fonte", "SELECT source AS fonte, count(*) AS noticias, count(*) FILTER (WHERE severity = 'critical') AS criticas FROM news_items WHERE $__timeFilter(published_at) GROUP BY 1 ORDER BY 2 DESC", h=7),
    ])  # fmt: skip
    health = dashboard("ta-health", "Saúde técnica", [
        stat("Segundos desde a última foto", "SELECT coalesce(extract(epoch FROM now() - max(at)), -1)::float AS atraso FROM telemetry_snapshots", "s"),
        stat("Intenções pendentes", "SELECT count(*) AS pendentes FROM intents WHERE status IN ('pending', 'unknown')"),
        stat("Posições sem proteção", "SELECT count(*) AS sem_protecao FROM positions WHERE state = 'unprotected'"),
        stat("Eventos críticos (24h)", "SELECT count(*) AS criticos FROM events WHERE severity = 'critical' AND created_at > now() - interval '24 hours'"),
        panel("Erros de API (%)", "SELECT at AS time, api_error_rate * 100 AS erros FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", unit="percent", w=8),
        panel("Peso usado (1 min)", "SELECT at AS time, used_weight_1m AS peso FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", w=8),
        panel("Offset de relógio (ms)", "SELECT at AS time, clock_offset_ms AS offset_ms FROM telemetry_snapshots WHERE $__timeFilter(at) ORDER BY 1", unit="ms", w=8),
        table("Eventos altos e críticos", "SELECT created_at AS time, kind AS evento, severity AS severidade, position_id AS posicao, payload::text AS detalhes FROM events WHERE severity IN ('high', 'critical') AND $__timeFilter(created_at) ORDER BY created_at DESC LIMIT 100"),
    ])  # fmt: skip
    search = {"name": "busca", "label": "Busca (regex)", "type": "textbox", "query": "", "current": {"text": "", "value": ""}}  # fmt: skip
    logs = dashboard("ta-logs", "Logs", [
        logql("Erros (24h)", 'sum(count_over_time({service=~"$service", level=~"error|critical|fatal"}[24h]))', kind="stat", instant=True, w=8, h=4),
        logql("Avisos (24h)", 'sum(count_over_time({service=~"$service", level=~"warning|warn"}[24h]))', kind="stat", instant=True, w=8, h=4),
        logql("Tracebacks do agente (24h)", 'sum(count_over_time({service="agent"} |~ "^Traceback" [24h]))', kind="stat", instant=True, w=8, h=4),
        logql("Linhas por nível", f"sum by (level) (count_over_time({FILTERED} [$__auto]))", legend="{{level}}"),
        logql("Avisos e erros por serviço", f'sum by (service) (count_over_time({{service=~"$service", {PROBLEMS}}} [$__auto]))', legend="{{service}}"),
        logql("Eventos do agente (período)", 'topk(15, sum by (event) (count_over_time({service="agent", level=~"$level"} | event != "" [$__range])))', kind="table", instant=True, w=8, h=12),
        logql("Avisos, erros e tracebacks", f'{{service=~"$service", {PROBLEMS}}}', kind="logs", w=16, h=12),
        logql("Logs", FILTERED, kind="logs", w=24, h=18),
    ], since="now-6h", variables=[
        label_variable("service", "Serviço", ".+"),
        label_variable("level", "Nível", ".*"),  # ".*" includes lines without a level
        search,
    ])  # fmt: skip
    return {
        "overview.json": overview,
        "positions.json": positions,
        "performance.json": performance,
        "research.json": research,
        "health.json": health,
        "logs.json": logs,
    }


def render(board: dict[str, Any]) -> str:
    return json.dumps(board, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, board in build().items():
        (OUTPUT / name).write_text(render(board), encoding="utf-8", newline="\n")
        print(f"{OUTPUT / name}")


if __name__ == "__main__":
    main()
