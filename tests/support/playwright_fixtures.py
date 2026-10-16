"""pytest-playwright's browser fixtures, scoped to a test module instead of the whole session.

Why: while a sync Playwright instance is alive, Playwright marks its asyncio loop as running
in the main thread between calls (``playwright/sync_api/_context_manager.py``). The plugin's
``playwright`` fixture is session-scoped, so after the first E2E test every later test of the
same run (``asyncio.run``, pytest-asyncio) failed with "cannot be called from a running event
loop": a full run in VS Code collects the E2E folders before ``tests/integration`` and
``tests/unit``. Here the driver starts with the first browser test of a module and stops
with its last one, so no other test ever runs while it is alive.

Usage, in an E2E suite's ``conftest.py`` (the names must be imported to override the plugin's)::

    from tests.support.playwright_fixtures import (  # noqa: F401 - fixtures
        browser,
        browser_context_args,
        browser_type,
        launch_browser,
        playwright,
    )

    @pytest.fixture(scope="session")
    def browser_context_overrides() -> dict[str, Any]:
        return {"viewport": {"width": 1600, "height": 900}}

The bodies follow pytest-playwright 0.10 (same options: ``--browser``, ``--headed``,
``--slowmo``, ``--device``, ``--video``, ``--tracing``, connect options).
"""

import json
import tempfile
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserType, Playwright, sync_playwright


@pytest.fixture(scope="module")
def playwright(_pw_api_request_contexts: Any) -> Iterator[Playwright]:
    pw = sync_playwright().start()
    if _pw_api_request_contexts:  # --tracing: also records the API requests
        _pw_api_request_contexts.instrument(pw)
    yield pw
    pw.stop()


@pytest.fixture(scope="module")
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    browser_type: BrowserType = getattr(playwright, browser_name)
    return browser_type


@pytest.fixture(scope="module")
def launch_browser(
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
) -> Callable[..., Browser]:
    def launch(**kwargs: Any) -> Browser:
        options = {**browser_type_launch_args, **kwargs}
        if connect_options:
            headers = {
                "x-playwright-launch-options": json.dumps(options),
                **(connect_options.get("headers") or {}),
            }
            return browser_type.connect(**{**connect_options, "headers": headers})
        return browser_type.launch(**options)

    return launch


@pytest.fixture(scope="module")
def browser(launch_browser: Callable[[], Browser]) -> Iterator[Browser]:
    browser = launch_browser()
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def browser_context_args(
    pytestconfig: pytest.Config,
    playwright: Playwright,
    device: str | None,
    base_url: str | None,
    _pw_artifacts_folder: tempfile.TemporaryDirectory[str],
    browser_context_overrides: dict[str, Any],
) -> dict[str, Any]:
    """The plugin's context arguments plus the suite's ``browser_context_overrides``."""
    args: dict[str, Any] = {}
    if device:
        args.update(playwright.devices[device])
    if base_url:
        args["base_url"] = base_url
    if pytestconfig.getoption("--video") in ("on", "retain-on-failure"):
        args["record_video_dir"] = _pw_artifacts_folder.name
    return args | browser_context_overrides
