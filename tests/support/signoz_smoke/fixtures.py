"""Fixtures and the collection hook of the SigNoz dashboard smoke tests.

They live here, typed and checked by mypy, and ``tests/signoz-dashboard-smoke-tests/conftest.py``
only re-exports them: the folder's name has dashes, so mypy names its files after their bare
file name, and two such folders (this one and the Grafana suite's) would both have a
``conftest`` module. See the suite's README.md.

``SIGNOZ_SMOKE_ATTACH`` names the JSON of a stack already running, with its credential and the
seed ingested (:class:`Attached`, written by the mutation tests, see tests/mutation/README.md):
the session uses it instead of starting its own, and applies the generator's dashboards again.
"""

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.support.signoz_smoke import environment, otlp, signoz
from tests.support.signoz_smoke.seed import MS, Telemetry, generate
from tests.support.signoz_smoke.ui import BrowserIssues

HERE = environment.HERE

MARKER = "signoz"  # registered in pyproject.toml, with the generic "e2e"
ATTACH_ENV = "SIGNOZ_SMOKE_ATTACH"


@dataclass(frozen=True, slots=True)
class Attached:
    """A stack started, registered and seeded once, for many sessions (``SIGNOZ_SMOKE_ATTACH``)."""

    project: str
    endpoints: environment.Endpoints
    credentials: signoz.Credentials
    seed: int
    hours: float
    end: datetime
    """The seed's end: ``generate(seed, hours, end)`` gives the same telemetry again."""

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), default=datetime.isoformat), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Attached":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            project=data["project"],
            endpoints=environment.Endpoints(**data["endpoints"]),
            credentials=signoz.Credentials(**data["credentials"]),
            seed=data["seed"],
            hours=data["hours"],
            end=datetime.fromisoformat(data["end"]),
        )


def attached() -> Attached | None:
    path = os.environ.get(ATTACH_ENV)
    return Attached.load(Path(path)) if path else None


def _requested(config: pytest.Config) -> bool:
    markexpr = config.option.markexpr or ""
    if MARKER in markexpr or "e2e" in markexpr:
        return True
    for arg in config.args:
        target = Path(arg.split("::", 1)[0]).resolve()
        if target == HERE or HERE in target.parents:
            return True
    return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Without asking for them, the e2e tests stay out of the run (fast, no Docker).

    ``--collect-only`` keeps them: it is how VS Code discovers the tests for its Testing panel
    (with ``tests`` as the argument), and it starts nothing."""
    if _requested(config) or config.option.collectonly:
        return
    keep: list[pytest.Item] = []
    drop: list[pytest.Item] = []
    for item in items:
        mine = HERE in Path(item.path).parents
        (drop if mine and item.get_closest_marker(MARKER) else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


# ================================================================== the stack and its data
@pytest.fixture(scope="session")
def signoz_stack() -> Iterator[environment.Endpoints]:
    if (running := attached()) is not None:
        yield running.endpoints
        return
    stack = environment.Stack()
    endpoints = stack.up()
    yield endpoints
    if not environment.keep():
        stack.down()


@pytest.fixture(scope="session")
def signoz_credentials(signoz_stack: environment.Endpoints) -> signoz.Credentials:
    if (running := attached()) is not None:  # the first admin exists already
        return running.credentials
    return signoz.bootstrap(signoz_stack.ui)


@pytest.fixture(scope="session")
def signoz_api(
    signoz_stack: environment.Endpoints, signoz_credentials: signoz.Credentials
) -> Iterator[httpx.Client]:
    with signoz.api(signoz_stack.ui, signoz_credentials) as http:
        yield http


@pytest.fixture(scope="session")
def dashboard_ids(signoz_api: httpx.Client) -> dict[str, str]:
    """``name`` → ``id`` of every dashboard of scripts/signoz_dashboards.py, applied."""
    if attached() is not None:  # a shared stack: none left by an earlier session
        signoz.remove_all(signoz_api)
    return signoz.provision(signoz_api)


@pytest.fixture(scope="session")
def telemetry(signoz_stack: environment.Endpoints, signoz_api: httpx.Client) -> Telemetry:
    """The seed (``SIGNOZ_SMOKE_SEED``, ``SIGNOZ_SMOKE_HOURS``), sent and fully ingested.

    ``signoz_api`` comes first on purpose: the ingester only accepts data once an organization
    exists (see :func:`environment.wait_otlp`)."""
    if (running := attached()) is not None:  # sent and ingested once, by whoever started it
        return generate(running.seed, running.hours, running.end)
    seed = int(os.environ.get("SIGNOZ_SMOKE_SEED", "7"))
    hours = float(os.environ.get("SIGNOZ_SMOKE_HOURS", "24"))
    data = generate(seed, hours)
    load_day(environment.Stack(), signoz_stack, signoz_api, data)
    return data


def load_day(
    stack: environment.Stack,
    endpoints: environment.Endpoints,
    http: httpx.Client,
    data: Telemetry,
) -> None:
    """Sends the seed to the ingester and waits until SigNoz holds all of it."""
    environment.wait_otlp(endpoints)
    otlp.send(data, endpoints.otlp)
    signoz.wait_ingested(http, data.summary, stack)


@pytest.fixture(scope="session")
def window(telemetry: Telemetry) -> tuple[int, int]:
    """The seeded window in ms (plus its last minute), for absolute time ranges."""
    return telemetry.summary.start // MS, telemetry.summary.end // MS + 60_000


# ================================================================== browser (pytest-playwright)
# Module scope, like the browser itself (tests/support/playwright_fixtures.py): a live sync
# Playwright keeps an asyncio loop marked as running, which broke every later test of the run.
@pytest.fixture(scope="module")
def signoz_login(
    browser: Browser,  # module scope: tests/support/playwright_fixtures.py
    signoz_stack: environment.Endpoints,
    signoz_credentials: signoz.Credentials,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Logs in through the login page once; the session (local storage) is reused."""
    context = browser.new_context(base_url=signoz_stack.ui)
    page = context.new_page()
    page.goto("/login")
    page.get_by_test_id("email").fill(signoz_credentials.email)
    page.get_by_test_id("initiate_login").click()
    page.get_by_test_id("password").fill(signoz_credentials.password)
    page.get_by_test_id("password_authn_submit").click()
    page.wait_for_url("**/home")
    state = tmp_path_factory.mktemp("signoz") / "state.json"
    context.storage_state(path=state)
    context.close()
    return state


@pytest.fixture(scope="module")
def browser_context_overrides(
    signoz_stack: environment.Endpoints, signoz_login: Path
) -> dict[str, Any]:
    return {
        "base_url": signoz_stack.ui,
        "storage_state": signoz_login,
        "viewport": {"width": 1600, "height": 1000},
        "timezone_id": "UTC",  # times and day boundaries as in the seed
        "locale": "en-US",  # number format: 4,789.5
    }


@pytest.fixture
def browser_issues(page: Page, signoz_stack: environment.Endpoints) -> Iterator[BrowserIssues]:
    """Every UI test ends checking the console and the network of its page."""
    issues = BrowserIssues(signoz_stack.ui)
    issues.attach(page)
    expect.set_options(timeout=30_000)
    try:
        yield issues
    finally:  # expect's options are global: the next suites keep Playwright's default
        expect.set_options(timeout=None)
    assert issues.problems == [], "\n".join(issues.problems)
