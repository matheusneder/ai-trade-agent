"""The ephemeral stack, the credential and the data, through the API (no browser)."""

import json

import httpx
import pytest
from scripts import signoz_dashboards
from signoz_smoke import environment, signoz
from signoz_smoke.seed import Telemetry

pytestmark = [pytest.mark.e2e, pytest.mark.signoz]


def test_isolation_guard_catches_the_production_names() -> None:
    """Foundry's manifests alone point at the real stack; with the override, nothing does."""
    stack = environment.Stack()
    files = ["-f", str(environment.BASE)]
    base = json.loads(
        environment._run("compose", "-p", stack.project, *files, "config", "--format", "json")
    )
    found = " ".join(environment.problems(base, stack.project))
    for name in ("signoz-signoz-0", "signoz-network", "signoz-telemetrystore-0-0-data", "8080"):
        assert name in found
    assert (
        environment.problems(json.loads(stack.compose("config", "--format", "json")), stack.project)
        == []
    )


def test_the_api_key_has_the_editor_role(
    signoz_stack: environment.Endpoints, signoz_credentials: signoz.Credentials
) -> None:
    headers = {"SIGNOZ-API-KEY": signoz_credentials.api_key}
    response = httpx.get(f"{signoz_stack.ui}/api/v2/dashboards", headers=headers)
    assert response.status_code == 200
    anonymous = httpx.get(f"{signoz_stack.ui}/api/v2/dashboards")
    assert anonymous.status_code == 401


def test_every_dashboard_is_provisioned(dashboard_ids: dict[str, str]) -> None:
    assert set(dashboard_ids) == {b["name"] for b in signoz_dashboards.build().values()}


def test_applying_again_updates_in_place(
    signoz_api: httpx.Client, dashboard_ids: dict[str, str]
) -> None:
    """``--apply`` is idempotent: same names, same ids."""
    assert signoz.provision(signoz_api) == dashboard_ids


def test_every_panel_query_returns_data(
    signoz_api: httpx.Client, dashboard_ids: dict[str, str], telemetry: Telemetry
) -> None:
    """``signoz_dashboards.py --check`` over the seed: no query fails or comes back empty."""
    hours = (telemetry.summary.end - telemetry.summary.start) / 3600e9
    assert signoz_dashboards.check(signoz_api, signoz_dashboards.build(), hours=hours) == 0
