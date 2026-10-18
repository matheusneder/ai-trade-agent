"""The interactive elements: time range picker, refresh, template variables, the regex search
and the panel view. Each interaction must refetch the panels without errors, and the new
values must still match the synthetic day."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.support.grafana_seed import Dataset, daily_scope_series
from tests.support.grafana_ui import (
    BOARDS,
    Watch,
    open_dashboard,
    panel,
    query_text,
    stat_text,
    wait_for_panels,
    wait_until,
)

GrafanaPage = tuple[Page, Watch]
TRADES = query_text(
    next(p for p in BOARDS["ta-performance"]["panels"] if p["title"] == "Trades")["targets"][0]
)


def test_time_range_picker_refetches_and_filters(grafana: GrafanaPage, seeded: Dataset) -> None:
    """``Last 6 hours`` counts only the trades closed in the window Grafana sent."""
    page, watch = grafana
    board = open_dashboard(page, watch, "ta-performance")
    watch.reset()
    page.get_by_test_id("data-testid TimePicker Open Button").click()
    page.get_by_text("Last 6 hours", exact=True).click()
    expect(page).to_have_url(re.compile("from=now\\-6h"))
    wait_for_panels(page, watch, board)
    result = watch.latest(TRADES)
    assert result is not None
    assert result.end - result.start == timedelta(hours=6)
    inside = [
        p for p in seeded.closed() if p.closed_at and result.start <= p.closed_at <= result.end
    ]
    assert result.value("trades") == len(inside)
    expect(panel(page, "Trades")).to_contain_text(str(len(inside)))
    assert len(inside) < seeded.expected.closed_trades  # the window really filtered


DAILY = query_text(
    next(
        p
        for p in BOARDS["ta-overview"]["panels"]
        if p["title"] == "Ganhos e perdas por escopo do dia (USDT)"
    )["targets"][0]
)


@pytest.mark.parametrize(
    "window",
    ["&from=now-1h&to=now", "&from=now-7d&to=now", "&from=now-3d&to=now-2d"],
    ids=["last-hour", "last-7-days", "two-days-ago"],
)
def test_daily_scope_pnl_ignores_the_time_range(
    grafana: GrafanaPage, seeded: Dataset, window: str
) -> None:
    """The day's chart asks for the UTC day so far whatever range the dashboard shows, even
    one that ends before today, and draws every scope."""
    page, watch = grafana
    open_dashboard(page, watch, "ta-overview", window)
    result = watch.latest(DAILY)
    assert result is not None and result.error is None
    now = datetime.now(UTC)
    assert result.start == now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert now - result.end < timedelta(minutes=2)
    expect(panel(page, "Ganhos e perdas por escopo do dia (USDT)")).to_contain_text("Today so far")
    chart = panel(page, "Ganhos e perdas por escopo do dia (USDT)")
    for scope in daily_scope_series(seeded, now):
        expect(chart.get_by_test_id(f"data-testid VizLegend series {scope}")).to_be_visible()


def test_refresh_button_runs_every_query_again(grafana: GrafanaPage) -> None:
    page, watch = grafana
    board = open_dashboard(page, watch, "ta-overview")
    watch.reset()
    page.get_by_test_id("data-testid RefreshPicker run button").click()
    wait_for_panels(page, watch, board)
    assert all(r.error is None and r.rows > 0 for r in watch.results)


def test_view_panel_opens_one_panel(grafana: GrafanaPage, seeded: Dataset) -> None:
    page, watch = grafana
    open_dashboard(page, watch, "ta-performance")
    panel(page, "Trades").hover()
    page.get_by_test_id("data-testid Panel menu Trades").click()
    page.get_by_role("menuitem", name="View").click()
    expect(page).to_have_url(re.compile("viewPanel="))
    expect(page.get_by_test_id("data-testid Panel header Trades")).to_be_visible()
    expect(panel(page, "Trades")).to_contain_text(str(seeded.expected.closed_trades))
    expect(page.get_by_test_id("data-testid Panel header Taxa de acerto")).to_have_count(0)


def test_service_variable_filters_the_logs(grafana: GrafanaPage) -> None:
    """Picking one service narrows the URL, the queries and the legend to it.

    Since Grafana 13 the multi-value picker keeps ``All`` checked when another value is
    checked (the selection still means every service), and Escape discards the pending
    change. A user unchecks ``All``, checks the service and leaves the picker, which applies
    the selection; the test does the same."""
    page, watch = grafana
    board = open_dashboard(page, watch, "ta-logs", "&from=now-24h&to=now")  # agent warnings
    all_values = "data-testid Dashboard template variables Variable Value DropDown value link text"
    picker = page.get_by_test_id(f"{all_values} $__all-input").first  # the first: Serviço
    picker.click()
    options = page.get_by_role("option").all_inner_texts()
    assert {"All", "agent", "grafana", "postgres", "loki", "alloy", "jaeger"} <= set(options)
    all_option = page.get_by_role("option", name="All", exact=True)
    expect(all_option).to_have_attribute("aria-selected", "true")
    all_option.click()
    page.get_by_role("option", name="agent", exact=True).click()
    expect(page.get_by_role("option", selected=True)).to_have_text(["agent"])  # agent only
    watch.reset()
    page.keyboard.press("Tab")  # leaving the picker applies the selection
    expect(page).to_have_url(re.compile("var\\-service=agent(&|$)"))
    wait_for_panels(page, watch, board, using="$service")
    by_service = next(r for r in reversed(watch.results) if "sum by (service)" in r.query)
    assert 'service=~"agent"' in by_service.query
    legend = panel(page, "Avisos e erros por serviço")
    expect(legend.get_by_test_id("data-testid VizLegend series agent")).to_be_visible()
    expect(legend.get_by_test_id("data-testid VizLegend series grafana")).to_have_count(0)


def test_regex_search_filters_the_log_lines(grafana: GrafanaPage) -> None:
    page, watch = grafana
    board = open_dashboard(page, watch, "ta-logs", "&from=now-24h&to=now")
    label = page.get_by_test_id(
        "data-testid Dashboard template variables submenu Label Busca (regex)"
    )
    search = label.locator("xpath=..").locator("input")
    watch.reset()
    # a backslash must reach Loki as is (the filter is a raw backtick string)
    search.fill(r"decision\.cycle")
    search.press("Enter")
    expect(page).to_have_url(re.compile("var\\-busca=decision"))
    wait_for_panels(page, watch, board, using="$busca")
    logs = panel(page, "Logs")
    expect(logs.get_by_text('"decision.cycle"').first).to_be_visible()
    expect(logs.get_by_text("telemetry.recorded")).to_have_count(0)
    expect(logs.get_by_text("Request Completed")).to_have_count(0)


def test_level_variable_reaches_the_queries(grafana: GrafanaPage) -> None:
    """The level selection (from the URL, as a shared link would carry it) reaches LogQL."""
    page, watch = grafana
    open_dashboard(page, watch, "ta-logs", "&var-level=error&from=now-24h&to=now")
    lines = next(r for r in reversed(watch.results) if r.query.startswith("sum by (level)"))
    assert 'level=~"error"' in lines.query
    legend = panel(page, "Linhas por nível")
    expect(legend.get_by_test_id("data-testid VizLegend series error")).to_be_visible()
    expect(legend.get_by_test_id("data-testid VizLegend series info")).to_have_count(0)
    wait_until(page, lambda: stat_text(page, "Erros (24h)") != "", lambda: "the errors stat")
