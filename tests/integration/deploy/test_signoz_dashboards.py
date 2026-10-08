"""SigNoz as code: dashboards in sync with the generator and within the v2 schema rules.

Validation on a real SigNoz 0.144 (creation through the API, which applies the schema
validator, and every query through ``/api/v5/query_range``) is done with ``--apply`` and
``--check``. The guarantees against regressions live here, mirroring the rules of SigNoz's
``pkg/types/dashboardtypes``.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from scripts import signoz_dashboards
from scripts.signoz_dashboards import (
    GRID_COLUMNS,
    OUTPUT,
    SCHEMA_VERSION,
    SigNozSettings,
    apply,
    build,
    check,
    client,
    render,
)

from trade_agent import tracing

ROOT = Path(__file__).parents[3]
METRICS_SOURCE = (ROOT / "src" / "trade_agent" / "metrics.py").read_text(encoding="utf-8")
COMPOSE: dict[str, Any] = yaml.safe_load(
    (ROOT / "deploy" / "stack.yml").read_text(encoding="utf-8")
)
BOARDS = build()
SIGNOZ = "http://signoz.test"
RESERVED_TAG_KEYS = {"name", "description", "created_at", "updated_at", "created_by", "locked"}
REQUEST_BY_PANEL = {  # the request type each panel type draws
    "signoz/TimeSeriesPanel": "time_series",
    "signoz/BarChartPanel": "time_series",
    "signoz/NumberPanel": "scalar",
    "signoz/ListPanel": "raw",
}


def _panels() -> Iterator[tuple[str, dict[str, Any]]]:
    for board in BOARDS.values():
        for panel_key, panel in board["spec"]["panels"].items():
            yield f"{board['name']}/{panel_key}", panel


def _builder_specs(panel: dict[str, Any]) -> list[dict[str, Any]]:
    plugin = panel["spec"]["queries"][0]["spec"]["plugin"]
    if plugin["kind"] == "signoz/CompositeQuery":
        return [q["spec"] for q in plugin["spec"]["queries"] if q["type"] == "builder_query"]
    return [plugin["spec"]]


# ============================================================================ generator
def test_versioned_dashboards_match_the_generator() -> None:
    assert sorted(p.name for p in OUTPUT.glob("*.json")) == sorted(BOARDS)
    for name, board in BOARDS.items():
        assert (OUTPUT / name).read_text(encoding="utf-8") == render(board), name


def test_dashboards_have_stable_valid_names() -> None:
    """The ``name`` identifies the dashboard when applying again: unique, a DNS label (RFC 1123)."""
    names = [board["name"] for board in BOARDS.values()]
    assert len(set(names)) == len(names) == 4
    for board in BOARDS.values():
        assert re.fullmatch(r"trade-agent-[a-z0-9-]+", board["name"])
        assert len(board["name"]) <= 63
        assert (board["schemaVersion"], board["generateName"]) == (SCHEMA_VERSION, False)
        assert not {tag["key"].lower() for tag in board["tags"]} & RESERVED_TAG_KEYS
        assert len(board["spec"]["display"]["name"]) <= 128


def test_layouts_fit_the_grid_and_place_every_panel_once() -> None:
    for board in BOARDS.values():
        spec = board["spec"]
        placed: list[str] = []
        for layout in spec["layouts"]:
            assert layout["kind"] == "Grid"
            items = layout["spec"]["items"]
            for item in items:
                assert min(item["width"], item["height"]) >= 1
                assert min(item["x"], item["y"]) >= 0
                assert item["x"] + item["width"] <= GRID_COLUMNS
                placed.append(item["content"]["$ref"].removeprefix("#/spec/panels/"))
            for i, a in enumerate(items):  # within a section, nothing overlaps
                for b in items[i + 1 :]:
                    assert not (
                        a["x"] < b["x"] + b["width"]
                        and b["x"] < a["x"] + a["width"]
                        and a["y"] < b["y"] + b["height"]
                        and b["y"] < a["y"] + a["height"]
                    ), (board["name"], a, b)
        assert sorted(placed) == sorted(spec["panels"])
        assert all(re.fullmatch(r"[a-zA-Z0-9_.-]+", key) for key in spec["panels"])


def test_each_panel_has_one_query_of_the_kind_it_draws() -> None:
    for name, panel in _panels():
        kind = panel["spec"]["plugin"]["kind"]
        (query,) = panel["spec"]["queries"]
        assert query["kind"] == REQUEST_BY_PANEL[kind], name
        specs = _builder_specs(panel)
        assert len({spec["name"] for spec in specs}) == len(specs), name
        if kind == "signoz/ListPanel":  # list: raw records from a single builder
            assert query["spec"]["plugin"]["kind"] == "signoz/BuilderQuery", name
            assert "aggregations" not in specs[0] and specs[0]["limit"] > 0, name
        if kind == "signoz/NumberPanel":  # a metric as a single value needs reduceTo
            for spec in specs:
                if spec["signal"] == "metrics":
                    assert all("reduceTo" in a for a in spec["aggregations"]), name
        for spec in specs:
            for aggregation in spec.get("aggregations", []):
                if "expression" in aggregation:  # a single function per aggregation
                    assert re.fullmatch(r"\w+\(\w*\)", aggregation["expression"]), name


def test_agent_metrics_exist() -> None:
    used = {
        aggregation["metricName"]
        for _, panel in _panels()
        for spec in _builder_specs(panel)
        for aggregation in spec.get("aggregations", [])
        if aggregation.get("metricName", "").startswith("trade_agent.")
    }
    defined = set(re.findall(r'"(trade_agent\.[\w.]+)"', METRICS_SOURCE))
    assert len(used) >= 15
    assert used <= defined, used - defined


def test_filters_name_real_components_and_compose_services() -> None:
    text = json.dumps(BOARDS)
    components = set(re.findall(r"service\.name = 'trade-agent\.(\w+)'", text))
    assert {"runtime", "exchange", "db", "llm"} <= components <= set(tracing.COMPONENTS)
    services = set(re.findall(r"service\.name = '([\w-]+)'", text))
    assert services <= set(COMPOSE["services"])  # in the logs, the name is the compose service


# ============================================================================ API
def _envelope(data: Any) -> dict[str, Any]:
    return {"status": "success", "data": data}


@pytest.fixture
def signoz() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=SIGNOZ, assert_all_called=False) as mock:
        yield mock


def test_settings_read_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TA_SIGNOZ_API_KEY", "chave-de-teste")
    settings = SigNozSettings(_env_file=None)
    assert settings.url == "http://127.0.0.1:8080"
    with client(settings) as http:
        assert http.headers["SIGNOZ-API-KEY"] == "chave-de-teste"
    assert "chave-de-teste" not in repr(settings)


def test_apply_creates_missing_and_updates_existing(signoz: respx.MockRouter) -> None:
    listed = [{"id": "abc", "name": "trade-agent-saude"}, {"id": "x", "name": "outro"}]
    signoz.get("/api/v2/dashboards").respond(200, json=_envelope({"dashboards": listed}))
    created = signoz.post("/api/v2/dashboards").respond(200, json=_envelope({"id": "novo"}))
    updated = signoz.put("/api/v2/dashboards/abc").respond(200, json=_envelope({}))
    with httpx.Client(base_url=SIGNOZ) as http:
        apply(http, BOARDS)
    assert (created.call_count, updated.call_count) == (len(BOARDS) - 1, 1)
    body = json.loads(updated.calls.last.request.content)
    assert body["name"] == "trade-agent-saude"
    assert "generateName" not in body  # the update refuses unknown fields
    posted = {json.loads(c.request.content)["name"] for c in created.calls}
    assert "trade-agent-saude" not in posted


def test_apply_surfaces_validation_errors(signoz: respx.MockRouter) -> None:
    signoz.get("/api/v2/dashboards").respond(200, json=_envelope({"dashboards": []}))
    signoz.post("/api/v2/dashboards").respond(400, json={"error": {"message": "x + width"}})
    with httpx.Client(base_url=SIGNOZ) as http, pytest.raises(RuntimeError, match="x \\+ width"):
        apply(http, BOARDS)


def _query_result(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    results: dict[str, list[dict[str, Any]]] = {
        "time_series": [{"aggregations": [{"series": [{"values": []}]}]}],
        "scalar": [{"data": [[1.0]]}],
        "raw": [{"rows": []}],  # lists without records
    }
    if "trade_agent.clock.offset" in request.content.decode():
        return httpx.Response(500, json={"error": "falhou"})
    data = {"type": body["requestType"], "data": {"results": results[body["requestType"]]}}
    return httpx.Response(200, json=_envelope(data))


def test_check_counts_failed_and_empty_queries(
    signoz: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    route = signoz.post("/api/v5/query_range").mock(side_effect=_query_result)
    with httpx.Client(base_url=SIGNOZ) as http:
        found = check(http, BOARDS, hours=1)
    panels = list(_panels())
    lists = sum(p["spec"]["plugin"]["kind"] == "signoz/ListPanel" for _, p in panels)
    clock = sum("trade_agent.clock.offset" in json.dumps(p) for _, p in panels)
    assert route.call_count == len(panels)
    assert found == lists + clock
    sent = json.loads(route.calls[0].request.content)
    assert sent["schemaVersion"] == "v1" and sent["end"] - sent["start"] == 3_600_000
    out = capsys.readouterr().out
    assert "ERRO" in out and "Últimos ciclos de decisão" in out


def test_main_writes_files_and_applies(
    signoz: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signoz_dashboards, "OUTPUT", tmp_path)
    monkeypatch.setenv("TA_SIGNOZ_API_KEY", "chave-de-teste")
    monkeypatch.setenv("TA_SIGNOZ_URL", SIGNOZ)
    monkeypatch.chdir(tmp_path)  # without the repository's .env
    signoz.get("/api/v2/dashboards").respond(200, json=_envelope({"dashboards": []}))
    created = signoz.post("/api/v2/dashboards").respond(200, json=_envelope({"id": "novo"}))
    signoz_dashboards.main(["--apply"])
    assert sorted(p.name for p in tmp_path.glob("*.json")) == sorted(BOARDS)
    assert created.call_count == len(BOARDS)
    assert created.calls[0].request.headers["SIGNOZ-API-KEY"] == "chave-de-teste"
