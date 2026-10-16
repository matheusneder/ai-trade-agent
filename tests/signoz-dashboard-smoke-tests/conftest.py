"""Fixtures of the SigNoz dashboard smoke tests (see README.md in this folder).

The tests marked ``signoz`` need Docker and Chromium, so a plain ``uv run pytest`` leaves them
out (deselected). They run when this folder (or a file or test in it) is named on the command
line, which is also what VS Code's test panel does, or with ``-m signoz`` (or ``-m e2e``).

One ephemeral stack serves the whole session: it starts, gets a credential, the dashboards and
the seed, and is destroyed at the end (``SIGNOZ_SMOKE_KEEP=1`` keeps it).
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.support.playwright_fixtures import (  # noqa: F401 - fixtures: a browser per module
    browser,
    browser_context_args,
    browser_type,
    launch_browser,
    playwright,
)
from tests.support.signoz_smoke import environment, otlp, signoz
from tests.support.signoz_smoke.seed import MS, Telemetry, generate
from tests.support.signoz_smoke.ui import BrowserIssues

HERE = Path(__file__).resolve().parent

MARKER = "signoz"  # registered in pyproject.toml, with the generic "e2e"


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
    stack = environment.Stack()
    endpoints = stack.up()
    yield endpoints
    if not environment.keep():
        stack.down()


@pytest.fixture(scope="session")
def signoz_credentials(signoz_stack: environment.Endpoints) -> signoz.Credentials:
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
    return signoz.provision(signoz_api)


@pytest.fixture(scope="session")
def telemetry(signoz_stack: environment.Endpoints, signoz_api: httpx.Client) -> Telemetry:
    """The seed (``SIGNOZ_SMOKE_SEED``, ``SIGNOZ_SMOKE_HOURS``), sent and fully ingested.

    ``signoz_api`` comes first on purpose: the ingester only accepts data once an organization
    exists (see :func:`environment.wait_otlp`)."""
    seed = int(os.environ.get("SIGNOZ_SMOKE_SEED", "7"))
    hours = float(os.environ.get("SIGNOZ_SMOKE_HOURS", "24"))
    data = generate(seed, hours)
    environment.wait_otlp(signoz_stack)
    otlp.send(data, signoz_stack.otlp)
    signoz.wait_ingested(signoz_api, data.summary)
    return data


@pytest.fixture(scope="session")
def window(telemetry: Telemetry) -> tuple[int, int]:
    """The seeded window in ms (plus its last minute), for absolute time ranges."""
    return telemetry.summary.start // MS, telemetry.summary.end // MS + 60_000


# ================================================================== browser (pytest-playwright)
# Module scope, like the browser itself (tests/support/playwright_fixtures.py): a live sync
# Playwright keeps an asyncio loop marked as running, which broke every later test of the run.
@pytest.fixture(scope="module")
def signoz_login(
    browser: Browser,  # noqa: F811 - the module-scoped fixture imported above
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
