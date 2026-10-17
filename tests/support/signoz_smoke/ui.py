"""Page objects for the SigNoz UI (v0.14x dashboards) and a watcher of browser errors.

Selectors rely on SigNoz's ``data-testid`` attributes and on ``data-panel-root``, which is the
panel key of ``scripts/signoz_dashboards.py`` (``equity-now``, ``cost``...). When a SigNoz
upgrade renames them, this is the only file to adjust.
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import ConsoleMessage, Locator, Page, Request, Response, Route, expect

# panel plugin kind (dashboard JSON) → element that only exists once the panel has drawn data
RENDERERS = {
    "signoz/TimeSeriesPanel": "time-series-renderer",
    "signoz/BarChartPanel": "bar-panel-renderer",
    "signoz/NumberPanel": "number-panel-value",
    "signoz/ListPanel": "list-panel-renderer",
}
# states a healthy panel never shows
FAILURES = (
    "panel-error",
    "panel-no-query",
    "number-panel-no-data",
    "error-empty-state",
    "no-results-empty-state",
)
# answers to the requests SigNoz's UI makes to other hosts, in the shape it expects: no newer
# release on GitHub, and the (empty) changelog of the running version from SigNoz's CMS
GITHUB_RELEASE = {"tag_name": "v0.0.0", "name": "v0.0.0"}
CORS = {"Access-Control-Allow-Origin": "*"}


def offline_answer(url: str) -> str:
    """Body for a request to another host (fonts and the like get an empty one)."""
    if "api.github.com" in url:
        return json.dumps(GITHUB_RELEASE)
    if "cms.signoz.cloud" in url:
        version = parse_qs(urlparse(url).query).get("filters[version][$eq]", [""])[0]
        entry = {"id": 1, "version": version, "release_date": "2026-01-01", "features": [],
                 "bug_fixes": None, "maintenance": None}  # fmt: skip
        return json.dumps({"data": [entry], "meta": {}})
    return ""


# known, harmless noise of a SigNoz without a license (the community edition)
ALLOWED_RESPONSES = {("/api/v4/licenses/active", 404)}
ALLOWED_CONSOLE = ("no active license found",)


@dataclass  # no slots: Playwright stores its wrappers on the handlers' instance
class BrowserIssues:
    """Console errors, uncaught exceptions and failed SigNoz requests seen by a page.

    Requests to other hosts (fonts, GitHub, SigNoz's CMS) never leave the machine: they get an
    empty answer here, so the tests do not depend on the internet.
    """

    origin: str
    problems: list[str] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)

    def attach(self, page: Page) -> None:
        page.route("**/*", self._route)
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.problems.append(f"uncaught: {error}"))
        page.on("response", self._response)
        page.on("requestfailed", self._failed)

    def _route(self, route: Route) -> None:
        url = route.request.url
        if url.startswith((self.origin, "data:")):
            route.continue_()
            return
        self.blocked.add(urlparse(url).netloc)
        json_api = route.request.resource_type in {"xhr", "fetch"}
        content_type = "application/json" if json_api else "text/plain"
        route.fulfill(status=200, body=offline_answer(url), content_type=content_type, headers=CORS)

    def _console(self, message: ConsoleMessage) -> None:
        if message.type != "error":
            return
        text = message.text
        # "Failed to load resource" repeats a response that _response/_failed already judge
        if text.startswith("Failed to load resource") or any(a in text for a in ALLOWED_CONSOLE):
            return
        where = message.location.get("url", "")
        self.problems.append(f"console: {text[:300]} ({where})")

    def _response(self, response: Response) -> None:
        path = urlparse(response.url).path
        if response.status >= 400 and (path, response.status) not in ALLOWED_RESPONSES:
            self.problems.append(f"HTTP {response.status} {response.request.method} {path}")

    def _failed(self, request: Request) -> None:
        failure = request.failure or ""
        # blocked hosts, and requests the UI itself cancels (a new time range, navigation)
        if not request.url.startswith(self.origin) or "ERR_ABORTED" in failure:
            return
        self.problems.append(f"failed: {request.method} {urlparse(request.url).path} {failure}")


@dataclass(slots=True)
class Dashboard:
    page: Page
    board: dict[str, Any]
    id: str

    @property
    def title(self) -> str:
        return str(self.board["spec"]["display"]["name"])

    @property
    def panels(self) -> dict[str, dict[str, Any]]:
        return dict(self.board["spec"]["panels"])

    def sections(self) -> Iterator[tuple[str, list[str]]]:
        """(section title, panel keys) in the order of the layout."""
        for layout in self.board["spec"]["layouts"]:
            keys = [item["content"]["$ref"].rsplit("/", 1)[-1] for item in layout["spec"]["items"]]
            yield layout["spec"]["display"]["title"], keys

    def open(
        self, *, start_ms: int | None = None, end_ms: int | None = None, relative: str = ""
    ) -> None:
        query = f"startTime={start_ms}&endTime={end_ms}" if start_ms else f"relativeTime={relative}"
        self.page.goto(f"/dashboard/{self.id}?{query}")
        expect(self.page.get_by_test_id("dashboard-title")).to_have_text(self.title)

    def panel(self, key: str) -> Locator:
        return self.page.locator(f'[data-panel-root="{key}"]')

    def kind(self, key: str) -> str:
        return str(self.panels[key]["spec"]["plugin"]["kind"])

    def rendered(self, key: str) -> Locator:
        """Scrolls to the panel (they load when visible) and waits until it drew its data."""
        panel = self.panel(key)
        panel.scroll_into_view_if_needed()
        expect(panel.get_by_test_id(RENDERERS[self.kind(key)]).first).to_be_visible()
        expect(panel.get_by_test_id("panel-loading")).to_have_count(0)
        for failure in FAILURES:
            expect(panel.get_by_test_id(failure), f"{key}: {failure}").to_have_count(0)
        return panel

    def number(self, key: str) -> str:
        """The value as shown, signed: SigNoz renders the minus sign in a prefix element
        (``-`` | ``115`` | ``ms``), so the value element alone reads 115 for -115."""
        panel = self.rendered(key)
        value = panel.get_by_test_id("number-panel-value").inner_text().strip()
        prefix = panel.get_by_test_id("value-display-prefix-unit")
        negative = prefix.count() > 0 and prefix.inner_text().strip() == "-"
        return f"-{value}" if negative else value

    def number_unit(self, key: str) -> str:
        """The unit shown after the value; SigNoz scales time units (1149 ms shows as 1 s)."""
        suffix = self.rendered(key).get_by_test_id("value-display-suffix-unit")
        return suffix.inner_text().strip() if suffix.count() else ""

    def legend(self, key: str) -> list[str]:
        return legend_labels(self.rendered(key))

    def rows(self, key: str) -> int:
        panel = self.rendered(key)
        rows = panel.locator("tbody tr.ant-table-row")
        expect(rows.first).to_be_visible()
        return rows.count()

    @contextmanager
    def queries(self) -> Iterator[list[dict[str, Any]]]:
        """Bodies of the ``/api/v5/query_range`` requests sent inside the block."""
        sent: list[dict[str, Any]] = []

        def record(request: Request) -> None:
            if request.url.endswith("/api/v5/query_range") and request.post_data:
                sent.append(json.loads(request.post_data))

        self.page.on("request", record)
        try:
            yield sent
        finally:
            self.page.remove_listener("request", record)


def legend_labels(scope: Locator) -> list[str]:
    """Every series a chart legend shows. The legend is a virtual list (only the visible rows
    exist), so it is scrolled to the end; its status line ("Showing 14 of 14 series", only
    with many series) must agree with what was found."""
    items = scope.locator('[data-testid^="legend-item-"]')
    expect(items.first).to_be_visible()
    found: set[str] = set()
    scroller = scope.locator("[data-virtuoso-scroller]").first
    for _ in range(100):
        found |= set(items.evaluate_all("rows => rows.map(r => r.getAttribute('aria-label'))"))
        if not scroller.count() or scroller.evaluate(_SCROLL_DOWN):
            break
        scope.page.wait_for_timeout(50)
    status = scope.get_by_test_id("legend-status")
    if status.count():
        shown = re.search(r"Showing (\d+) of", status.inner_text())
        assert shown and int(shown.group(1)) == len(found), (status.inner_text(), found)
    return sorted(found)


# scrolls one screen; true when it was already at the end
_SCROLL_DOWN = """e => {
    const end = e.scrollTop + e.clientHeight >= e.scrollHeight - 1;
    e.scrollTop += e.clientHeight;
    return end;
}"""


def number_text(value: float, decimals: int = 0) -> str:
    """How a number panel shows a count: thousands separators."""
    return f"{round(value, decimals):,.{decimals}f}"


TIME_UNITS = {"ns": 1e-6, "µs": 1e-3, "ms": 1.0, "s": 1e3, "min": 6e4, "m": 6e4, "h": 3.6e6}
"""Milliseconds per unit, for the time units SigNoz may switch to when it formats a value."""


def unit_scale(shown: str, configured: str) -> float:
    """How many ``configured`` units one ``shown`` unit is (1 when no time scaling happened)."""
    if shown in TIME_UNITS and configured in TIME_UNITS:
        return TIME_UNITS[shown] / TIME_UNITS[configured]
    return 1.0


def number_value(text: str) -> float:
    """The number a panel shows ("4,789.5" → 4789.5)."""
    return float(text.replace(",", ""))
