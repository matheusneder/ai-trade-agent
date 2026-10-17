"""Fixtures and the collection hook of the Grafana dashboard smoke tests.

They live here, typed and checked by mypy, and ``tests/grafana-dashboard-smoke-tests/conftest.py``
only re-exports them: the folder's name has dashes, so mypy names its files after their bare
file name, and two such folders (this one and the SigNoz suite's) would both have a
``conftest`` module. See the suite's README.md.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.support.grafana_seed import Dataset, generate, insert, loki_streams
from tests.support.grafana_stack import SUITE, Stack, ephemeral_stack, push_logs, run_async
from tests.support.grafana_ui import Watch

HERE = SUITE
E2E_FILES = {
    "test_grafana_provisioning.py",
    "test_grafana_dashboards.py",
    "test_grafana_interactions.py",
}


def _targeted(config: pytest.Config) -> bool:
    """Whether the run asked for this suite: a path inside it, or a marker expression."""
    if any(word in (config.option.markexpr or "") for word in ("e2e", "grafana")):
        return True
    for arg in config.args:
        path = Path(arg.split("::")[0]).resolve()
        if path == HERE or HERE in path.parents:
            return True
    return False


def pytest_itemcollected(item: pytest.Item) -> None:
    """The markers, as soon as each test is collected: ``-m`` filters on them afterwards."""
    if HERE in item.path.parents:
        item.add_marker(pytest.mark.grafana)
        if item.path.name in E2E_FILES:
            item.add_marker(pytest.mark.e2e)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselects the E2E tests unless the run asked for them (after ``-m`` filtering)."""
    mine = [item for item in items if HERE in item.path.parents]
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
