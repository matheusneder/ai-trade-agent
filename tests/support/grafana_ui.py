"""Playwright helpers for the Grafana dashboard smoke tests.

Two independent views of each panel: the DOM (what the user sees: no "No data", no error
badge) and the ``/api/ds/query`` responses the page received (the rows behind the panel,
matched to it by its query). The selectors come from Grafana's ``@grafana/e2e-selectors``
(``data-testid ...``), which Grafana keeps stable across versions for its own E2E tests.
"""

import functools
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import ConsoleMessage, Locator, Page, Request, Response, expect
from scripts.grafana_dashboards import build

BOARDS: dict[str, dict[str, Any]] = {board["uid"]: board for board in build().values()}
POSTGRES_UIDS = [uid for uid in BOARDS if uid != "ta-logs"]
NO_DATA = "No data"


def query_text(target: dict[str, Any]) -> str:
    """The query of a panel target as written in the dashboard (SQL or LogQL)."""
    return str(target.get("rawSql") or target.get("expr"))


VARIABLE = re.compile(r"\\\$\w+")  # a ``$name`` after re.escape


@functools.cache
def _pattern(template: str) -> re.Pattern[str]:
    """Matches the query Grafana sends for ``template``: the browser interpolates the
    dashboard variables (``$service``, ``$__auto``...) and leaves the SQL macros to the
    server, so every ``$name`` may be either itself or its value."""
    return re.compile(VARIABLE.sub(".*?", re.escape(template)) + "$", re.S)


def sent_for(template: str, query: str) -> bool:
    return _pattern(template).match(query) is not None


@dataclass(slots=True)
class Result:
    """One query of one ``/api/ds/query`` call: the request window and the returned columns."""

    query: str
    start: datetime
    end: datetime
    status: int
    error: str | None
    frames: list[dict[str, list[Any]]]

    @property
    def rows(self) -> int:
        return sum(len(next(iter(frame.values()), [])) for frame in self.frames)

    def column(self, name: str) -> list[Any]:
        return [v for frame in self.frames for v in frame.get(name, [])]

    def value(self, name: str) -> Any:
        values = self.column(name)
        assert len(values) == 1, f"{name}: {values}"
        return values[0]


def _frames(result: dict[str, Any]) -> list[dict[str, list[Any]]]:
    frames = []
    for frame in result.get("frames", []):
        names = [f["name"] for f in frame["schema"]["fields"]]
        values = frame.get("data", {}).get("values", [])
        columns = dict(zip(names, values, strict=False))
        rows = len(values[-1]) if values else 0
        for f in frame["schema"]["fields"]:  # Loki series: the labels are columns too
            for label, value in (f.get("labels") or {}).items():
                columns.setdefault(label, [value] * rows)
        frames.append(columns)
    return frames


@dataclass
class Watch:
    """Everything the page reported: queries, console errors and failed requests."""

    origin: str
    results: list[Result] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.console_errors.append(f"pageerror: {error}"))
        page.on("requestfailed", self._request_failed)
        page.on("response", self._response)

    def _console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.console_errors.append(message.text)

    def _request_failed(self, request: Request) -> None:
        # ERR_ABORTED: Grafana cancels a query it no longer needs (a newer one replaced it)
        aborted = request.failure == "net::ERR_ABORTED"
        if urlparse(request.url).netloc == urlparse(self.origin).netloc and not aborted:
            self.failed.append(f"{request.method} {request.url}: {request.failure}")

    def _response(self, response: Response) -> None:
        if urlparse(response.url).netloc != urlparse(self.origin).netloc:
            return
        if response.status >= 400:
            self.failed.append(f"{response.status} {response.request.method} {response.url}")
        if "/api/ds/query" in response.url and response.request.method == "POST":
            self._record(response)

    def _record(self, response: Response) -> None:
        sent = json.loads(response.request.post_data or "{}")
        try:
            body = response.json()
        except Exception:  # an unreadable body is reported as a query error
            body = {}
        start = datetime.fromtimestamp(int(sent.get("from", 0)) / 1000, UTC)
        end = datetime.fromtimestamp(int(sent.get("to", 0)) / 1000, UTC)
        for query in sent.get("queries", []):
            result = body.get("results", {}).get(query["refId"], {})
            self.results.append(
                Result(
                    query=query_text(query),
                    start=start,
                    end=end,
                    status=result.get("status", response.status),
                    error=result.get("error") or (None if body else "unreadable response"),
                    frames=_frames(result),
                )
            )

    def latest(self, template: str) -> Result | None:
        """The last result of the panel query ``template`` (as written in the dashboard)."""
        return next((r for r in reversed(self.results) if sent_for(template, r.query)), None)

    def reset(self) -> None:
        self.results.clear()

    def problems(self) -> list[str]:
        return self.console_errors + self.failed


def wait_until(
    page: Page, condition: Callable[[], bool], what: Callable[[], str], timeout_s: float = 45
) -> None:
    """Polls ``condition`` through Playwright's own wait: the sync API only dispatches the
    page's events (the responses ``Watch`` records) while a Playwright call runs."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        page.wait_for_timeout(250)
    raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what()}")


def panel(page: Page, title: str) -> Locator:
    """The panel container (header, menu and visualization) with this title."""
    header = page.get_by_test_id(f"data-testid Panel header {title}")
    return page.locator("[data-viz-panel-key]").filter(has=header)


def open_dashboard(page: Page, watch: Watch, uid: str, params: str = "") -> dict[str, Any]:
    """Opens the dashboard and waits until every panel received its query results."""
    board = BOARDS[uid]
    watch.reset()
    page.goto(f"/d/{uid}?orgId=1{params}")
    expect(page.get_by_test_id(f"data-testid {board['title']} breadcrumb")).to_be_visible()
    wait_for_panels(page, watch, board)
    return board


def wait_for_panels(
    page: Page, watch: Watch, board: dict[str, Any], *, using: str | None = None
) -> None:
    """Scrolls through the panels (Grafana loads them lazily) until all have results.

    ``using="$service"``: only the panels whose query uses that variable (after it changed,
    the others keep their results and send nothing)."""
    for p in board["panels"]:
        panel(page, p["title"]).scroll_into_view_if_needed()
    expected = [query_text(t) for p in board["panels"] for t in p["targets"]]
    expected = [q for q in expected if using is None or using in q]

    def missing() -> list[str]:
        return [t for t in expected if watch.latest(t) is None]

    wait_until(page, lambda: not missing(), lambda: f"the queries of {board['uid']}: {missing()}")
    page.wait_for_load_state("networkidle")


def stat_text(page: Page, title: str) -> str:
    """The value(s) a stat panel shows, without its title."""
    text = panel(page, title).inner_text().strip()
    return text.removeprefix(title).strip()
