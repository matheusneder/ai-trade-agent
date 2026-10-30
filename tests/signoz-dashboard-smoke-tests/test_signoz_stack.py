"""The ephemeral stack, the credential and the data, through the API (no browser)."""

import json
import time
from typing import Any

import httpx
import pytest
from scripts import signoz_dashboards

from tests.support.signoz_smoke import environment, seed, signoz
from tests.support.signoz_smoke.seed import Telemetry

pytestmark = [pytest.mark.e2e, pytest.mark.signoz]


@pytest.mark.dashboards
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


@pytest.mark.dashboards
def test_the_api_key_has_the_editor_role(
    signoz_stack: environment.Endpoints, signoz_credentials: signoz.Credentials
) -> None:
    headers = {"SIGNOZ-API-KEY": signoz_credentials.api_key}
    response = httpx.get(f"{signoz_stack.ui}/api/v2/dashboards", headers=headers)
    assert response.status_code == 200
    anonymous = httpx.get(f"{signoz_stack.ui}/api/v2/dashboards")
    assert anonymous.status_code == 401


@pytest.mark.dashboards("*")
def test_every_dashboard_is_provisioned(dashboard_ids: dict[str, str]) -> None:
    assert set(dashboard_ids) == {b["name"] for b in signoz_dashboards.build().values()}


@pytest.mark.dashboards("*")
def test_applying_again_updates_in_place(
    signoz_api: httpx.Client, dashboard_ids: dict[str, str]
) -> None:
    """``--apply`` is idempotent: same names, same ids."""
    assert signoz.provision(signoz_api) == dashboard_ids


@pytest.mark.dashboards("*")
def test_every_panel_query_returns_data(
    signoz_api: httpx.Client, dashboard_ids: dict[str, str], telemetry: Telemetry
) -> None:
    """``signoz_dashboards.py --check`` over the seed: no query fails or comes back empty."""
    hours = (telemetry.summary.end - telemetry.summary.start) / 3600e9
    assert signoz_dashboards.check(signoz_api, signoz_dashboards.build(), hours=hours) == 0


# ================================================================== the day's result per scope
DAILY = signoz_dashboards.build()["trade-agent-operacao.json"]["spec"]["panels"]["pnl-scope-today"]
DAILY_SQL = DAILY["spec"]["queries"][0]["spec"]["plugin"]["spec"]


def _daily(
    http: httpx.Client, *, at: int | None = None, hours: float = 24
) -> dict[str, list[tuple[int, float]]]:
    """The panel's query as SigNoz runs it, for the range ``hours`` before now (or ``at``):
    scope → [(minute in ms, value)]. ``at`` (unix ns, whole seconds) evaluates it at that
    instant instead of ``now()``."""
    spec = dict(DAILY_SQL)
    end = int(time.time() * 1000)
    if at is not None:
        spec["query"] = spec["query"].replace("now()", f"toDateTime({at // seed.SECOND})")
        end = at // seed.MS
    body = {
        "schemaVersion": "v1",
        "start": end - int(hours * 3_600_000),
        "end": end,
        "requestType": "time_series",
        "compositeQuery": {"queries": [{"type": "clickhouse_sql", "spec": spec}]},
    }
    response = http.post("/api/v5/query_range", json=body)
    assert response.status_code == 200, response.text
    (result,) = response.json()["data"]["data"]["results"]
    found: dict[str, list[tuple[int, float]]] = {}
    for aggregation in result.get("aggregations") or []:
        for line in aggregation["series"]:
            (label,) = line["labels"]
            assert label["key"]["name"] == "scope"
            found[label["value"]] = [(v["timestamp"], v["value"]) for v in line["values"]]
    return found


def _same(found: dict[str, list[tuple[int, float]]], expected: dict[str, Any]) -> None:
    """Same series, minutes and values; the querier rounds values to 3 decimals."""
    assert set(found) == set(expected)
    for scope, points in expected.items():
        assert [t for t, _ in found[scope]] == [t for t, _ in points], scope
        assert [v for _, v in found[scope]] == pytest.approx(
            [v for _, v in points], abs=0.0005 + 1e-9
        ), scope


@pytest.mark.dashboards("trade-agent-operacao")
def test_daily_scope_pnl_is_the_day_so_far(signoz_api: httpx.Client, telemetry: Telemetry) -> None:
    expected = telemetry.summary.daily_scope_series(time.time_ns())
    found = _daily(signoz_api)
    _same(found, expected)
    assert all(points[0][1] == 0 for points in found.values())  # the day starts at zero


@pytest.mark.dashboards("trade-agent-operacao")
@pytest.mark.parametrize("hours", [1, 6, 7 * 24])
def test_daily_scope_pnl_ignores_the_time_range(
    signoz_api: httpx.Client, telemetry: Telemetry, hours: float
) -> None:
    """The dashboard's range reaches the query (start/end) and changes nothing."""
    _same(_daily(signoz_api, hours=hours), telemetry.summary.daily_scope_series(time.time_ns()))


@pytest.mark.dashboards("trade-agent-operacao")
def test_daily_scope_pnl_restarts_at_the_utc_midnight(
    signoz_api: httpx.Client, telemetry: Telemetry
) -> None:
    """Evaluated around the seeded midnight: the day before keeps its series up to 23:59:59;
    right after midnight there is none until the first reading, which starts at zero."""
    summary = telemetry.summary
    midnight = summary.end - summary.end % seed.DAY
    after = [at for at, _ in summary.scope_pnl["global"] if at >= midnight]
    if not after:
        pytest.skip("the seed ended less than a minute after midnight")
    before = midnight - seed.SECOND
    _same(_daily(signoz_api, at=before), summary.daily_scope_series(before))
    assert _daily(signoz_api, at=midnight + seed.SECOND) == {}
    # the seed reads at whole seconds, like the query's instant
    first = _daily(signoz_api, at=after[0])
    assert first == {scope: [(midnight // seed.MS, 0.0)] for scope in seed.SCOPES}
