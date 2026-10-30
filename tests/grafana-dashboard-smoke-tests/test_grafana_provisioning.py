"""Grafana's API on the ephemeral stack: provisioned dashboards, data sources and alert rules."""

import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml

from tests.support.grafana_seed import Dataset
from tests.support.grafana_stack import ROOT, Stack
from tests.support.grafana_ui import BOARDS

RULES = yaml.safe_load(
    (ROOT / "deploy" / "grafana" / "provisioning" / "alerting" / "rules.yaml").read_text("utf-8")
)


@pytest.fixture(scope="module")
def api(stack: Stack, seeded: Dataset) -> Iterator[httpx.Client]:
    auth = (stack.admin_user, stack.admin_password)
    with httpx.Client(base_url=stack.grafana_url, auth=auth, timeout=30) as client:
        yield client


@pytest.mark.dashboards("*")
def test_every_versioned_dashboard_is_provisioned(api: httpx.Client) -> None:
    found = api.get("/api/search", params={"tag": "trade-agent"}).raise_for_status().json()
    assert {d["uid"] for d in found} == set(BOARDS)
    assert {d["folderTitle"] for d in found} == {"Trade Agent"}
    for uid, board in BOARDS.items():
        stored = api.get(f"/api/dashboards/uid/{uid}").raise_for_status().json()
        assert stored["meta"]["provisioned"] is True
        assert [p["title"] for p in stored["dashboard"]["panels"]] == [
            p["title"] for p in board["panels"]
        ]


@pytest.mark.dashboards
@pytest.mark.parametrize("uid", ["trade-agent-pg", "trade-agent-loki"])
def test_data_sources_are_healthy(api: httpx.Client, uid: str) -> None:
    """The versioned provisioning reaches the ephemeral services (grafana_ro on PostgreSQL)."""
    health = api.get(f"/api/datasources/uid/{uid}/health").raise_for_status().json()
    assert health["status"] == "OK", health


def _rule_states(api: httpx.Client) -> dict[str, dict[str, Any]]:
    data = api.get("/api/prometheus/grafana/api/v1/rules").raise_for_status().json()
    rules = [r for g in data["data"]["groups"] for r in g["rules"]]
    return {r["labels"].get("__alert_rule_uid__", r["name"]): r for r in rules}


@pytest.mark.dashboards
def test_alert_rules_evaluate_with_data_and_stay_normal(api: httpx.Client) -> None:
    """The three rules read the seeded tables: fresh telemetry, no unprotected position and a
    drawdown under 10 %. Grafana evaluates them every minute; the first evaluation may
    precede the seed, so this waits for a clean one."""
    expected = {r["title"] for g in RULES["groups"] for r in g["rules"]}
    deadline = time.monotonic() + 180
    while True:
        rules = list(_rule_states(api).values())
        if {r["name"] for r in rules} == expected and all(
            r["health"] == "ok" and r["state"] == "inactive" for r in rules
        ):
            return
        if time.monotonic() > deadline:
            pytest.fail(
                f"alert rules not normal: {[(r['name'], r['health'], r['state']) for r in rules]}"
            )
        time.sleep(5)
