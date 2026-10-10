"""Grafana as code: dashboards in sync with the generator and queries valid on the real schema.

The check on a real Grafana 12.4 (provisioning, the 42 panel queries through the API, the
rules and the Jaeger source: services, search and dependency graph) was done manually; the
guarantees against regressions live here.
The logs dashboard (Loki) is tested in ``test_logs.py``.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.grafana_dashboards import DATASOURCE, LOKI, OUTPUT, build, render
from sqlalchemy import text

from trade_agent.persistence.db import Database

DEPLOY = Path(__file__).parents[3] / "deploy"
PROVISIONING = DEPLOY / "grafana" / "provisioning"
TIME_FILTER = re.compile(r"\$__timeFilter\((\w+)\)")


def _grafana_sql(sql: str) -> str:
    """Expands the Grafana macros used in the panels (7-day window)."""
    return TIME_FILTER.sub(r"\1 BETWEEN now() - interval '7 days' AND now()", sql)


def _dashboard_queries() -> list[tuple[str, str]]:
    return [
        (f"{board['title']} / {panel['title']}", target["rawSql"])
        for board in build().values()
        for panel in board["panels"]
        for target in panel["targets"]
        if target["datasource"] == DATASOURCE  # the LogQL queries run in test_logs.py
    ]


def _rules() -> list[dict[str, Any]]:
    data = yaml.safe_load((PROVISIONING / "alerting" / "rules.yaml").read_text(encoding="utf-8"))
    return [rule for group in data["groups"] for rule in group["rules"]]


def test_versioned_dashboards_match_the_generator() -> None:
    boards = build()
    assert sorted(p.name for p in OUTPUT.glob("*.json")) == sorted(boards)
    for name, board in boards.items():
        assert (OUTPUT / name).read_text(encoding="utf-8") == render(board), name
        ids = [p["id"] for p in board["panels"]]
        assert ids == list(range(1, len(ids) + 1))
        assert all(p["gridPos"]["x"] + p["gridPos"]["w"] <= 24 for p in board["panels"])
        expected = LOKI if name == "logs.json" else DATASOURCE
        assert all(t["datasource"] == expected for p in board["panels"] for t in p["targets"])
        assert all(p["datasource"] == expected for p in board["panels"])
    assert len(boards) == 6
    assert len({b["uid"] for b in boards.values()}) == 6


@pytest.mark.parametrize(("name", "sql"), _dashboard_queries())
async def test_panel_queries_run_on_the_schema(db: Database, name: str, sql: str) -> None:
    async with db.engine.connect() as conn:
        await conn.execute(text(_grafana_sql(sql)))


async def test_alert_rule_queries_run_on_the_schema(db: Database) -> None:
    rules = _rules()
    assert {r["uid"] for r in rules} == {"ta-no-telemetry", "ta-unprotected", "ta-drawdown"}
    async with db.engine.connect() as conn:
        for rule in rules:
            query, reduce, threshold = rule["data"]
            assert query["datasourceUid"] == DATASOURCE["uid"]
            assert {reduce["datasourceUid"], threshold["datasourceUid"]} == {"__expr__"}
            assert rule["condition"] == threshold["refId"] == "C"
            result = await conn.execute(text(query["model"]["rawSql"]))
            assert len(result.all()) <= 1  # no rows = NoData (handled by noDataState)


def test_datasource_and_telegram_placeholder_match_compose() -> None:
    datasource = yaml.safe_load(
        (PROVISIONING / "datasources" / "postgres.yaml").read_text(encoding="utf-8")
    )["datasources"][0]
    assert (datasource["uid"], datasource["type"]) == (DATASOURCE["uid"], DATASOURCE["type"])
    assert datasource["user"] == "grafana_ro"
    contact = yaml.safe_load((PROVISIONING / "alerting" / "telegram.yaml").read_text("utf-8"))
    settings = contact["contactPoints"][0]["receivers"][0]["settings"]
    assert settings["chatid"] == "__TELEGRAM_CHAT_ID__"  # rendered at startup (text)
    compose = yaml.safe_load((DEPLOY / "stack.yml").read_text(encoding="utf-8"))
    grafana = compose["services"]["grafana"]
    assert "s/__TELEGRAM_CHAT_ID__/$${TELEGRAM_CHAT_ID}/" in grafana["command"][0]
    provisioning = grafana["environment"]["GF_PATHS_PROVISIONING"]
    assert provisioning == "/tmp/provisioning"  # noqa: S108 - path inside the container
    init = (DEPLOY / "postgres" / "init" / "10-grafana-readonly.sh").read_text(encoding="utf-8")
    assert "ALTER DEFAULT PRIVILEGES" in init and "grafana_ro" in init
    assert json.loads(render(build()["overview.json"]))["refresh"] == "1m"


def test_compose_always_reads_the_root_env_file() -> None:
    """Without the include, ``docker compose -f deploy/docker-compose.yml`` (no ``--env-file``)
    looked for the .env in deploy/ and started Grafana with grafana_ro's default password."""
    entry = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert entry == {
        "name": "trade-agent",
        "include": [{"path": "stack.yml", "env_file": "../.env"}],
    }
    stack = (DEPLOY / "stack.yml").read_text(encoding="utf-8")
    assert not re.search(r"^name:", stack, re.M)  # the project name lives in the entry file
    example = (DEPLOY.parent / ".env.example").read_text(encoding="utf-8")
    for variable in ("GRAFANA_DB_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "TA_TELEGRAM_BOT_TOKEN"):
        assert f"${{{variable}:-" in stack and f"\n{variable}=" in example
