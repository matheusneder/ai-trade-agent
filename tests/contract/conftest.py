"""Contract tests against external APIs: the live services, over the network.

The default ``uv run pytest`` deselects them (they need the network and depend on a third
party's uptime); they run when the run names this folder, a file or a test in it, or selects
the marker (``-m contract``). See README.md.
"""

from pathlib import Path

import pytest

HERE = Path(__file__).parent


def _targeted(config: pytest.Config) -> bool:
    if "contract" in (config.option.markexpr or ""):
        return True
    for arg in config.args:
        path = Path(arg.split("::")[0]).resolve()
        if path == HERE or HERE in path.parents:
            return True
    return False


def pytest_itemcollected(item: pytest.Item) -> None:
    if HERE in item.path.parents:
        item.add_marker(pytest.mark.contract)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselects the contract tests unless the run asked for them (after ``-m`` filtering)."""
    if _targeted(config) or config.option.collectonly:  # collect-only: VS Code's discovery
        return
    mine = [item for item in items if HERE in item.path.parents]
    if mine:
        config.hook.pytest_deselected(items=mine)
        items[:] = [item for item in items if item not in mine]
