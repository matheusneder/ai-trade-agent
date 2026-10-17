"""Each dashboard in the browser: it loads, every panel renders data, and the values match
the synthetic day (``Dataset.expected``, computed from the generated rows, not by SQL).

The ``grafana`` fixture fails the test on any console error or failed request (4xx/5xx).
"""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.support.grafana_seed import Dataset, log_counts
from tests.support.grafana_ui import (
    BOARDS,
    NO_DATA,
    Result,
    Watch,
    open_dashboard,
    panel,
    query_text,
    stat_text,
)

GrafanaPage = tuple[Page, Watch]


def _result(watch: Watch, uid: str, title: str) -> Result:
    target = next(p for p in BOARDS[uid]["panels"] if p["title"] == title)["targets"][0]
    result = watch.latest(query_text(target))
    assert result is not None, title
    return result


@pytest.mark.parametrize("uid", list(BOARDS))
def test_dashboard_loads_with_data_in_every_panel(grafana: GrafanaPage, uid: str) -> None:
    page, watch = grafana
    board = open_dashboard(page, watch, uid)
    for p in board["panels"]:
        title = p["title"]
        result = _result(watch, uid, title)
        assert result.status == 200 and result.error is None, (title, result.error)
        assert result.rows > 0, f"{title}: the query returned no rows"
        element = panel(page, title)
        expect(element).to_be_visible()
        expect(element.get_by_text(NO_DATA, exact=True)).to_have_count(0)
        expect(element.get_by_test_id("data-testid Panel status error")).to_have_count(0)
        if p["type"] == "timeseries":  # drawn series: one legend entry per series
            expect(
                element.locator('[data-testid^="data-testid VizLegend series"]').first
            ).to_be_visible()
        if p["type"] == "table":
            expect(element.get_by_role("row").nth(1)).to_be_visible()  # header + data


def test_overview_matches_the_seed(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    expected = seeded.expected
    open_dashboard(page, watch, "ta-overview")

    def value(title: str, column: str) -> float:
        return float(_result(watch, "ta-overview", title).value(column))

    assert value("Patrimônio", "patrimonio") == pytest.approx(expected.equity, abs=1e-4)
    assert value("PnL do dia", "pnl_dia") == pytest.approx(expected.day_pnl, abs=1e-4)
    assert value("Drawdown", "drawdown") == pytest.approx(expected.drawdown_pct)
    assert value("Posições ativas", "posicoes") == expected.active_positions
    assert value("Posições ativas", "exposicao") == pytest.approx(expected.exposure, abs=1e-6)
    assert stat_text(page, "Posições ativas").split()[1] == str(expected.active_positions)
    states = _result(watch, "ta-overview", "Estado operacional")
    assert (
        dict(zip(states.column("escopo"), states.column("estado"), strict=True)) == expected.states
    )
    changes = _result(watch, "ta-overview", "Mudanças de estado (risco)")
    assert changes.column("estado") == ["running", "paused"]  # most recent first
    tasks = _result(watch, "ta-overview", "Última execução por tarefa").column("tarefa")
    assert set(tasks) == {"agent.started", "decision.cycle", "risk.state_changed"}
    scopes = panel(page, "Ganhos e perdas por escopo (USDT)")
    for scope in expected.states:  # global and each profile: one series each
        expect(scopes.get_by_test_id(f"data-testid VizLegend series {scope}")).to_be_visible()


def test_positions_match_the_seed(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    open_dashboard(page, watch, "ta-positions")
    active = _result(watch, "ta-positions", "Posições ativas")
    open_symbols = sorted(p.symbol for p in seeded.positions if p.state == "protected")
    assert sorted(active.column("ativo")) == open_symbols
    assert set(active.column("estado")) == {"protected"}
    for symbol in open_symbols:
        expect(panel(page, "Posições ativas").get_by_text(symbol, exact=True)).to_be_visible()
    closed = _result(watch, "ta-positions", "Encerradas no período")
    assert closed.rows == seeded.expected.closed_trades
    assert sum(closed.column("pnl")) == pytest.approx(seeded.expected.realized_total, abs=1e-6)
    exposure = _result(watch, "ta-positions", "Exposição").column("exposicao")
    assert exposure[-1] == pytest.approx(seeded.expected.exposure, abs=1e-6)


def test_performance_matches_the_seed(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    expected = seeded.expected
    open_dashboard(page, watch, "ta-performance")

    def single(title: str, column: str) -> float:
        return float(_result(watch, "ta-performance", title).value(column))

    assert single("Trades", "trades") == expected.closed_trades
    assert stat_text(page, "Trades") == str(expected.closed_trades)
    assert single("Taxa de acerto", "acerto") == pytest.approx(expected.win_rate_pct)
    assert single("Profit factor", "profit_factor") == pytest.approx(expected.profit_factor)
    assert single("Taxas pagas (USDT)", "taxas") == pytest.approx(expected.fees_usdt, abs=1e-8)
    cumulative = _result(watch, "ta-performance", "PnL realizado acumulado").column("pnl_acumulado")
    assert cumulative[-1] == pytest.approx(expected.realized_total, abs=1e-6)
    reasons = _result(watch, "ta-performance", "Motivos de saída")
    assert dict(zip(reasons.column("motivo"), reasons.column("trades"), strict=True)) == (
        expected.exit_reasons
    )
    groups = _result(watch, "ta-performance", "Por perfil e ativo")
    found = zip(
        groups.column("perfil"), groups.column("ativo"), groups.column("trades"), strict=True
    )
    assert {(p, s): n for p, s, n in found} == expected.by_profile_symbol


def test_research_matches_the_seed(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    expected = seeded.expected
    open_dashboard(page, watch, "ta-research")
    cycles = _result(watch, "ta-research", "Ciclos de decisão")
    assert cycles.rows == expected.decision_cycles
    analyst = _result(watch, "ta-research", "Última leitura do analista")
    assert analyst.column("ativo") == expected.analyst_assets
    regime = _result(watch, "ta-research", "Regime e exposição")
    assert regime.value("regime") == expected.regime
    assert regime.value("exposicao") == pytest.approx(expected.exposure_multiplier)
    status = _result(watch, "ta-research", "Ciclos do analista por status")
    assert (sum(status.column("ok")), sum(status.column("falhas"))) == (
        expected.reports_ok,
        expected.reports_failed,
    )
    cost = _result(watch, "ta-research", "Custo diário do LLM").column("custo")
    assert sum(cost) == pytest.approx(expected.llm_cost, abs=1e-6)
    news = _result(watch, "ta-research", "Notícias coletadas por fonte")
    assert dict(zip(news.column("fonte"), news.column("noticias"), strict=True)) == (
        expected.news_by_source
    )
    assert sum(news.column("criticas")) >= 1
    for source in expected.news_by_source:
        expect(
            panel(page, "Notícias coletadas por fonte").get_by_text(source, exact=True)
        ).to_be_visible()


def test_health_matches_the_seed(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    open_dashboard(page, watch, "ta-health")
    now = datetime.now(UTC)

    def single(title: str, column: str) -> float:
        return float(_result(watch, "ta-health", title).value(column))

    delay = single("Segundos desde a última foto", "atraso")
    assert 0 <= delay <= (now - seeded.end).total_seconds() + 5
    assert single("Intenções pendentes", "pendentes") == seeded.expected.pending_intents
    assert single("Posições sem proteção", "sem_protecao") == seeded.expected.unprotected
    critical = [
        e for e in seeded.events()
        if e.severity == "critical" and e.created_at > now - timedelta(hours=24)
    ]  # fmt: skip
    assert single("Eventos críticos (24h)", "criticos") == len(critical)
    assert stat_text(page, "Eventos críticos (24h)") == str(len(critical))
    high = _result(watch, "ta-health", "Eventos altos e críticos")
    assert high.rows == sum(e.severity in {"high", "critical"} for e in seeded.events())
    errors = _result(watch, "ta-health", "Erros de API (%)").column("erros")
    assert max(errors) > 20  # the burst that paused the global scope


def test_logs_match_the_seed(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    open_dashboard(page, watch, "ta-logs")
    counts = log_counts(seeded, datetime.now(UTC) - timedelta(hours=24))
    for title, key in (
        ("Erros (24h)", "errors"),
        ("Avisos (24h)", "warnings"),
        ("Tracebacks do agente (24h)", "tracebacks"),
    ):
        assert stat_text(page, title) == str(counts[key]), title
    events = _result(watch, "ta-logs", "Eventos do agente (período)")
    assert "decision.cycle" in events.column("event")
    logs = panel(page, "Logs")
    expect(logs.get_by_text("telemetry.recorded").first).to_be_visible()
