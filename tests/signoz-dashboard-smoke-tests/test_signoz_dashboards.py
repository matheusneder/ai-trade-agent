"""End-to-end smoke tests of the SigNoz dashboards, in Chromium, over the seeded day.

The panels, sections and series come from ``scripts/signoz_dashboards.py`` itself, so a new
panel is already checked for rendering and errors. Its *values* are not: add them to
``EXPECTED_NUMBERS``/``EXPECTED_LEGENDS``/``EXPECTED_ROWS`` below (and the facts they need to
``tests.support.signoz_smoke.seed.Summary``). See README.md.
"""

import re
from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from scripts import signoz_dashboards

from tests.support.signoz_smoke.seed import Summary, Telemetry
from tests.support.signoz_smoke.ui import (
    BrowserIssues,
    Dashboard,
    legend_labels,
    number_text,
    number_value,
    unit_scale,
)

pytestmark = [pytest.mark.e2e, pytest.mark.signoz]

BOARDS = {board["name"]: board for board in signoz_dashboards.build().values()}

type Expect = Callable[[Summary], Any]

# panel key → value the number panel must show for the seeded window: counts exactly, other
# values up to one unit of the last decimal shown (SigNoz picks how many, up to the panel's
# decimal precision)
EXPECTED_NUMBERS: dict[str, Callable[[Summary], float]] = {
    # Operação (gauges: the last reading)
    "equity-now": lambda s: s.last["trade_agent.equity"],
    "pnl-now": lambda s: s.last["trade_agent.pnl.daily"],
    "drawdown-now": lambda s: s.last["trade_agent.drawdown"],
    "exposure-now": lambda s: s.last["trade_agent.exposure"],
    "positions-now": lambda s: s.last["trade_agent.positions.active"],
    "risk-now": lambda s: s.last["trade_agent.risk.state"],
    # Saúde técnica (counts over the period are exact)
    "weight-now": lambda s: s.last["trade_agent.binance.weight_used_1m"],
    "clock-now": lambda s: s.last["trade_agent.clock.offset"],
    "log-errors-now": lambda s: s.error_logs,
    "job-failures-now": lambda s: s.job_failures,
    "requests-now": lambda s: s.exchange_requests,
    # LLM (the counters' increase over the window adds up every call)
    "cost-now": lambda s: s.llm_cost,
    "tokens-now": lambda s: s.llm_tokens,
    "calls-now": lambda s: s.llm_calls,
    # Contêineres
    "containers-now": lambda s: len(s.containers),
}

# panel key → series the legend must list (exactly)
EXPECTED_LEGENDS: dict[str, Expect] = {
    # Operação
    "equity": lambda s: {"patrimônio", "abertura do dia", "pico"},
    "pnl": lambda s: {"do dia", "realizado", "aberto"},
    "pnl-scope": lambda s: {"global", "swing_trend", "momentum_alpha"},
    "risk-state": lambda s: {"global", "swing_trend", "momentum_alpha"},
    "state-changes": lambda s: set(s.state_changes),
    "cycles": lambda s: set(s.decision_cycles),
    "entries-exits": lambda s: (
        {f"entradas {p}" for p in s.entries} | {f"saídas {p}" for p in s.exits}
    ),
    # Saúde técnica
    "http-errors": lambda s: {f"HTTP {status}" for status in s.exchange_http_errors},
    "job-latency": lambda s: set(s.jobs),
    "job-failures": lambda s: set(s.failed_jobs),
    "spans": lambda s: set(s.services),
    "problems": lambda s: set(s.problem_logs),
    # LLM
    "cost": lambda s: set(s.llm_series),
    "tokens": lambda s: {"input", "output"},
    "calls": lambda s: set(s.llm_series),
    "latency": lambda s: {"p50", "p95"},
    # Contêineres
    **{
        key: (lambda s: set(s.containers))
        for key in ("cpu", "memory", "rx", "tx", "disk", "memory-limit")
    },
    "log-volume": lambda s: set(s.log_services),
}

# list panel key → rows it must show
EXPECTED_ROWS: dict[str, Expect] = {
    "last-cycles": lambda s: min(50, sum(s.decision_cycles.values())),
    "llm-calls": lambda s: min(30, s.llm_calls),
}


def _panels(*kinds: str) -> list[Any]:
    return [
        pytest.param(name, key, id=f"{name}:{key}")
        for name, board in BOARDS.items()
        for key, panel in board["spec"]["panels"].items()
        if not kinds or panel["spec"]["plugin"]["kind"] in kinds
    ]


@pytest.fixture
def open_board(
    page: Page,
    browser_issues: BrowserIssues,
    dashboard_ids: dict[str, str],
    telemetry: Telemetry,
    window: tuple[int, int],
) -> Callable[[str], Dashboard]:
    """Opens a dashboard on the seeded window (absolute range: exact totals)."""

    def open_it(name: str) -> Dashboard:
        board = Dashboard(page, BOARDS[name], dashboard_ids[name])
        board.open(start_ms=window[0], end_ms=window[1])
        return board

    return open_it


# ================================================================== listing and loading
def test_dashboard_list_shows_every_dashboard(
    page: Page, browser_issues: BrowserIssues, dashboard_ids: dict[str, str]
) -> None:
    page.goto("/dashboard")
    for board in BOARDS.values():
        expect(page.get_by_text(board["spec"]["display"]["name"], exact=True)).to_be_visible()


@pytest.mark.parametrize("name", BOARDS)
def test_dashboard_loads_with_every_section_and_panel(
    open_board: Callable[[str], Dashboard], name: str
) -> None:
    board = open_board(name)
    expect(board.page.get_by_test_id("dashboard-tags")).to_contain_text("trade-agent")
    for title, keys in board.sections():
        first = board.page.get_by_test_id(f"dashboard-section-toggle-sec-{keys[0]}")
        expect(first).to_contain_text(title)
        for key in keys:
            header = board.panel(key).locator(".panel-drag-handle")
            expect(header).to_contain_text(board.panels[key]["spec"]["display"]["name"])


@pytest.mark.parametrize("name", BOARDS)
def test_every_panel_renders_its_data(open_board: Callable[[str], Dashboard], name: str) -> None:
    """Each panel draws (chart, number or table) with no error, no "no data" state."""
    board = open_board(name)
    for _, keys in board.sections():
        for key in keys:
            panel = board.rendered(key)
            kind = board.kind(key)
            if kind in {"signoz/TimeSeriesPanel", "signoz/BarChartPanel"}:
                expect(panel.get_by_test_id("uplot-main-div")).to_be_visible()
                assert board.legend(key), key
            elif kind == "signoz/NumberPanel":
                assert board.number(key) not in {"", "-", "NaN"}, key
            else:
                assert board.rows(key) > 0, key


# ================================================================== seeded values
@pytest.mark.parametrize(("name", "key"), _panels("signoz/NumberPanel"))
def test_number_panels_show_the_seeded_values(
    open_board: Callable[[str], Dashboard], telemetry: Telemetry, name: str, key: str
) -> None:
    board = open_board(name)
    shown = board.number(key)
    if key not in EXPECTED_NUMBERS:  # depends on the last minute's noise (CPU, p95...)
        assert number_value(shown) >= 0
        return
    expected = EXPECTED_NUMBERS[key](telemetry.summary)
    formatting = board.panels[key]["spec"]["plugin"]["spec"]["formatting"]
    decimals = len(shown.partition(".")[2])
    precision = int(formatting["decimalPrecision"])
    # below one unit of the last decimal SigNoz keeps significant digits (-0.0013, not -0.00)
    assert decimals <= precision or abs(number_value(shown)) < 10**-precision, shown
    # the seeded values change with the window's end: negative, or large enough for SigNoz
    # to show another time unit (1149 ms as "1 s"), on some runs
    scale = unit_scale(board.number_unit(key), formatting["unit"])
    if scale == 1 and float(expected).is_integer():
        assert shown == number_text(expected)
    else:
        assert number_value(shown) * scale == pytest.approx(expected, abs=scale * 10**-decimals)


@pytest.mark.parametrize(("name", "key"), _panels("signoz/TimeSeriesPanel", "signoz/BarChartPanel"))
def test_chart_legends_list_the_seeded_series(
    open_board: Callable[[str], Dashboard], telemetry: Telemetry, name: str, key: str
) -> None:
    board = open_board(name)
    found = set(board.legend(key))
    if key in EXPECTED_LEGENDS:
        assert found == EXPECTED_LEGENDS[key](telemetry.summary)
    else:  # top-N panels (endpoints, database operations) or a single unnamed series
        limit = board.panels[key]["spec"]["queries"][0]["spec"]["plugin"]["spec"].get("limit")
        assert 0 < len(found) <= (limit or len(found))


@pytest.mark.parametrize(("name", "key"), _panels("signoz/ListPanel"))
def test_list_panels_show_the_seeded_rows(
    open_board: Callable[[str], Dashboard], telemetry: Telemetry, name: str, key: str
) -> None:
    board = open_board(name)
    rows = board.rows(key)
    if key in EXPECTED_ROWS:
        assert rows == EXPECTED_ROWS[key](telemetry.summary)
    else:
        assert rows > 0
    fields = board.panels[key]["spec"]["plugin"]["spec"]["selectFields"]
    header = board.panel(key).locator("thead")
    for field in fields:
        expect(header).to_contain_text(field["name"])


def test_every_expectation_points_at_a_real_panel() -> None:
    """A renamed panel key must not leave its expectation silently unused."""
    keys = {key for board in BOARDS.values() for key in board["spec"]["panels"]}
    assert set(EXPECTED_NUMBERS) | set(EXPECTED_LEGENDS) | set(EXPECTED_ROWS) <= keys


# ================================================================== interaction
def _url_has(parameter: str) -> re.Pattern[str]:
    return re.compile(rf"[?&]{re.escape(parameter)}(&|$)")


@pytest.mark.parametrize("name", BOARDS)
def test_time_range_selector_reloads_the_panels(
    open_board: Callable[[str], Dashboard], name: str
) -> None:
    board = open_board(name)
    page = board.page
    first = next(iter(board.panels))
    board.rendered(first)
    for label, relative, minutes in (("Last 6 hours", "6h", 360), ("Last 1 day", "1d", 1440)):
        with board.queries() as sent:
            page.locator(".timeSelection-input").click()
            page.get_by_text(label, exact=True).click()
            expect(page).to_have_url(_url_has(f"relativeTime={relative}"))
            expect(page.locator(".timeSelection-input .time-input-prefix")).to_have_text(relative)
            board.rendered(first)
        assert sent, "no query after changing the time range"
        # SigNoz aligns start and end to the millisecond: 6 h may be 359.99 minutes
        assert {round((q["end"] - q["start"]) / 60_000) for q in sent} == {minutes}


def test_shorter_range_shows_fewer_requests(
    open_board: Callable[[str], Dashboard], telemetry: Telemetry
) -> None:
    board = open_board("trade-agent-saude")
    total = number_text(telemetry.summary.exchange_requests)
    assert board.number("requests-now") == total
    board.page.locator(".timeSelection-input").click()
    board.page.get_by_text("Last 1 hour", exact=True).click()
    expect(board.page).to_have_url(_url_has("relativeTime=1h"))
    value = board.panel("requests-now").get_by_test_id("number-panel-value")
    expect(value).not_to_have_text(total)
    assert 0 < number_value(value.inner_text()) < number_value(total)


@pytest.mark.parametrize("name", BOARDS)
def test_refresh_button_runs_the_queries_again(
    open_board: Callable[[str], Dashboard], name: str
) -> None:
    """The refresh button only exists for relative ranges ("last 1 day")."""
    board = open_board(name)
    board.open(relative="1d")
    first = next(iter(board.panels))
    board.rendered(first)
    query = re.compile(r"/api/v5/query_range$")
    with board.queries() as sent, board.page.expect_request(query):
        board.page.locator(".refresh-actions .refresh-btn button").click()
    board.rendered(first)
    assert {q["end"] - q["start"] for q in sent} == {86_400_000}


@pytest.mark.parametrize("name", BOARDS)
def test_sections_collapse_and_expand(open_board: Callable[[str], Dashboard], name: str) -> None:
    board = open_board(name)
    for _, keys in board.sections():
        toggle = board.page.get_by_test_id(f"dashboard-section-toggle-sec-{keys[0]}")
        board.rendered(keys[0])
        toggle.click()
        expect(board.panel(keys[0])).to_have_count(0)
        toggle.click()
        board.rendered(keys[0])


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("trade-agent-operacao", "equity"),
        ("trade-agent-saude", "http-errors"),
        ("trade-agent-llm", "latency"),
        ("trade-agent-conteineres", "cpu"),
    ],
)
def test_panel_view_opens_the_chart_full_size(
    open_board: Callable[[str], Dashboard], telemetry: Telemetry, name: str, key: str
) -> None:
    board = open_board(name)
    page = board.page
    board.rendered(key)
    page.get_by_test_id(f"panel-actions-{key}").click()
    page.get_by_role("menuitem", name="View").click()
    dialog = page.get_by_test_id("view-panel-modal-content")
    expect(page).to_have_url(_url_has(f"expandedWidgetId={key}"))
    expect(dialog.get_by_test_id("uplot-main-div")).to_be_visible()
    assert set(legend_labels(dialog)) == EXPECTED_LEGENDS[key](telemetry.summary)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(page).not_to_have_url(re.compile("expandedWidgetId="))


@pytest.mark.parametrize(
    ("name", "key"), [("trade-agent-operacao", "equity"), ("trade-agent-llm", "tokens")]
)
def test_legend_hides_and_shows_a_series(
    open_board: Callable[[str], Dashboard], name: str, key: str
) -> None:
    board = open_board(name)
    board.legend(key)
    item = board.panel(key).get_by_test_id("legend-item-1")
    marker = item.get_by_test_id("legend-marker-1")
    expect(item).to_have_attribute("aria-checked", "true")
    marker.click()
    expect(item).to_have_attribute("aria-checked", "false")
    marker.click()
    expect(item).to_have_attribute("aria-checked", "true")


@pytest.mark.parametrize(("term", "key"), [("postgres", "cpu"), ("signoz", "memory")])
def test_legend_search_filters_the_series(
    open_board: Callable[[str], Dashboard], telemetry: Telemetry, term: str, key: str
) -> None:
    """Charts with many series (14 containers) get a search box over the legend."""
    board = open_board("trade-agent-conteineres")
    everything = board.legend(key)
    search = board.panel(key).get_by_test_id("legend-search-input")
    search.fill(term)
    matching = [label for label in everything if term in label]
    status = board.panel(key).get_by_test_id("legend-status")
    expect(status).to_have_text(f"Showing {len(matching)} of {len(everything)} series")
    assert board.legend(key) == matching
    search.fill("")
    expect(status).to_have_text(f"Showing {len(everything)} of {len(everything)} series")


def _variables() -> list[Any]:
    found = [
        pytest.param(name, variable, id=f"{name}:{variable['spec']['name']}")
        for name, board in BOARDS.items()
        for variable in board["spec"]["variables"]
    ]
    return found or [pytest.param(None, None, marks=pytest.mark.skip("no dashboard variables yet"))]


@pytest.mark.parametrize(("name", "variable"), _variables())
def test_variables_render_their_selector(
    open_board: Callable[[str], Dashboard], name: str, variable: dict[str, Any]
) -> None:
    """Placeholder that grows with the dashboards: each variable must show its selector."""
    board = open_board(name)
    bar = board.page.get_by_test_id("dashboard-variables-bar")
    expect(bar).to_contain_text(variable["spec"]["name"])


def test_seeded_window_is_the_time_range(
    open_board: Callable[[str], Dashboard], window: tuple[int, int]
) -> None:
    """The absolute range in the URL reaches the queries unchanged."""
    board = open_board("trade-agent-llm")
    with board.queries() as sent:
        board.page.reload()
        board.rendered("calls-now")
    assert sent
    assert {(q["start"], q["end"]) for q in sent} == {window}
