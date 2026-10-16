"""Fixtures of the Grafana dashboard smoke tests (see README.md in this folder).

The session starts an ephemeral PostgreSQL + Loki + Grafana (``compose.yaml``), migrates the
database with the project's Alembic migrations, writes the synthetic day of
``tests.support.grafana_seed`` and pushes its logs to Loki. Everything is removed at the end.

The E2E tests (Docker and a browser) only run when the run targets this folder, e.g.
``uv run pytest tests/grafana-dashboard-smoke-tests`` or a test of it in VS Code, or with
``-m e2e``/``-m grafana``; a plain ``uv run pytest`` deselects them. ``test_seed.py`` needs
neither and always runs.

Environment: ``GRAFANA_SMOKE_SEED`` (default 42) and ``GRAFANA_SMOKE_HOURS`` (default 24)
choose the synthetic day; ``GRAFANA_SMOKE_KEEP=1`` keeps the stack after the run.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.support.grafana_seed import Dataset, generate, insert, loki_streams
from tests.support.grafana_stack import Stack, ephemeral_stack, push_logs, run_async
from tests.support.grafana_ui import Watch
from tests.support.playwright_fixtures import (  # noqa: F401 - fixtures: a browser per module
    browser,
    browser_context_args,
    browser_type,
    launch_browser,
    playwright,
)

HERE = Path(__file__).parent
E2E_FILES = {"test_provisioning.py", "test_dashboards.py", "test_interactions.py"}


def _targeted(config: pytest.Config) -> bool:
    """Whether the run asked for this suite: a path inside it, or a marker expression."""
    if any(word in (config.option.markexpr or "") for word in ("e2e", "grafana")):
        return True
    for arg in config.args:
        path = Path(arg.split("::")[0]).resolve()
        if path == HERE or HERE in path.parents:
            return True
    return False


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    mine = [item for item in items if HERE in item.path.parents]
    for item in mine:
        item.add_marker(pytest.mark.grafana)
        if item.path.name in E2E_FILES:
            item.add_marker(pytest.mark.e2e)
    if _targeted(config) or config.option.collectonly:  # collect-only: VS Code's discovery
        return
    skipped = [item for item in mine if item.get_closest_marker("e2e")]
    if skipped:
        config.hook.pytest_deselected(items=skipped)
        items[:] = [item for item in items if item not in skipped]


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    yield from ephemeral_stack()


@pytest.fixture(scope="session")
def seeded(stack: Stack) -> Dataset:
    """The synthetic day in the ephemeral PostgreSQL and Loki."""
    data = generate(
        seed=int(os.environ.get("GRAFANA_SMOKE_SEED", "42")),
        hours=int(os.environ.get("GRAFANA_SMOKE_HOURS", "24")),
    )
    run_async(insert(stack.database_url, data))
    push_logs(stack.loki_url, loki_streams(data))
    return data


@pytest.fixture(scope="session")
def base_url(stack: Stack) -> str:
    """pytest-playwright's base URL: ``page.goto("/d/...")`` opens the ephemeral Grafana."""
    return stack.grafana_url


@pytest.fixture(scope="session")
def browser_context_overrides() -> dict[str, Any]:
    # tall enough for every panel of the longest dashboard (Grafana loads panels lazily)
    return {"viewport": {"width": 1600, "height": 2400}, "timezone_id": "UTC", "locale": "en-US"}


@pytest.fixture
def grafana(page: Page, stack: Stack, seeded: Dataset) -> Iterator[tuple[Page, Watch]]:
    """A logged-in page with its watcher. At the end, the page must have had no console
    errors and no failed request (4xx/5xx, ``/api/ds/query`` included)."""
    response = page.request.post(
        "/login", data={"user": stack.admin_user, "password": stack.admin_password}
    )
    assert response.ok, response.text()
    watch = Watch(stack.grafana_url)
    watch.attach(page)
    yield page, watch
    assert watch.problems() == []
