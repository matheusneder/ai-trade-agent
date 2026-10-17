"""Fixtures of the SigNoz dashboard smoke tests (see README.md in this folder).

The tests marked ``signoz`` need Docker and Chromium, so a plain ``uv run pytest`` leaves them
out (deselected). They run when this folder (or a file or test in it) is named on the command
line, which is also what VS Code's test panel does, or with ``-m signoz`` (or ``-m e2e``).

One ephemeral stack serves the whole session: it starts, gets a credential, the dashboards and
the seed, and is destroyed at the end (``SIGNOZ_SMOKE_KEEP=1`` keeps it).
"""

# The fixtures and the hook are defined in tests/support/signoz_smoke/fixtures.py (typed, checked
# by mypy); pytest registers them here, in the folder they apply to. This file stays out of mypy:
# see the "exclude" of [tool.mypy] in pyproject.toml.
from tests.support.playwright_fixtures import (  # noqa: F401 - fixtures: a browser per module
    browser,
    browser_context_args,
    browser_type,
    launch_browser,
    playwright,
)
from tests.support.signoz_smoke.fixtures import (  # noqa: F401 - fixtures and the hook, re-exported
    browser_context_overrides,
    browser_issues,
    dashboard_ids,
    pytest_collection_modifyitems,
    signoz_api,
    signoz_compose,
    signoz_credentials,
    signoz_login,
    signoz_stack,
    telemetry,
    window,
)
