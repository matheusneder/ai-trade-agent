"""Fixtures of the Grafana dashboard smoke tests (see README.md in this folder).

The session starts an ephemeral PostgreSQL + Loki + Grafana (``compose.yaml``), migrates the
database with the project's Alembic migrations, writes the synthetic day of
``tests.support.grafana_seed`` and pushes its logs to Loki. Everything is removed at the end.

The E2E tests (Docker and a browser) only run when the run targets this folder, e.g.
``uv run pytest tests/grafana-dashboard-smoke-tests`` or a test of it in VS Code, or with
``-m e2e``/``-m grafana``; a plain ``uv run pytest`` deselects them. ``test_grafana_seed.py``
and ``test_grafana_stack_hygiene.py`` need neither and always run. ``--collect-only`` (VS Code's
discovery) keeps them all.

The browser fixtures come from ``tests/support/playwright_fixtures.py``: module scope, so
Playwright never outlives the module's browser tests (see that module's docstring).

Environment: ``GRAFANA_SMOKE_SEED`` (default 42) and ``GRAFANA_SMOKE_HOURS`` (default 24)
choose the synthetic day; ``GRAFANA_SMOKE_KEEP=1`` keeps the stack until the next run.
"""

# The fixtures and the hook are defined in tests/support/grafana_fixtures.py (typed, checked by
# mypy); pytest registers them here, in the folder they apply to. This file stays out of mypy:
# see the "exclude" of [tool.mypy] in pyproject.toml.
from tests.support.grafana_fixtures import (  # noqa: F401 - fixtures and the hook, re-exported
    base_url,
    browser_context_overrides,
    grafana,
    pytest_collection_modifyitems,
    pytest_itemcollected,
    seeded,
    stack,
)
from tests.support.playwright_fixtures import (  # noqa: F401 - fixtures: a browser per module
    browser,
    browser_context_args,
    browser_type,
    launch_browser,
    playwright,
)
