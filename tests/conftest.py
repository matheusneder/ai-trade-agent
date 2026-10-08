import os
from collections.abc import Iterator

import pytest
import structlog

from tests.support.metrics import Measured, measuring
from tests.support.tracing import Recorded, recording
from trade_agent import metrics, tracing


@pytest.fixture
def spans() -> Iterator[Recorded]:
    """Spans of every component, recorded in memory during the test."""
    with recording() as recorded:
        yield recorded


@pytest.fixture
def measured() -> Iterator[Measured]:
    """The agent's metrics, read in memory during the test."""
    with measuring() as found:
        yield found


@pytest.fixture(autouse=True)
def _no_tracing_leak() -> Iterator[None]:
    """No test inherits (or leaves behind) installed tracing or metrics."""
    tracing.install(None)
    metrics.install(None)
    yield
    tracing.install(None)
    metrics.install(None)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps ``TA_*`` variables from the developer's environment from leaking into the tests."""
    for name in list(os.environ):
        if name.startswith(("TA_", "ANTHROPIC_")):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Each test starts with the default structlog (no level filter): CLI tests call
    ``configure_logging``, and the global configuration must not leak into the others."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marks tests by directory: ``tests/integration`` and ``tests/live``."""
    for item in items:
        parts = item.path.parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "live" in parts:
            item.add_marker(pytest.mark.live)
