"""Logs dos contêineres: Alloy → Loki → Grafana, com as configurações versionadas em deploy/.

O pipeline roda de verdade: um Alloy com o ``loki.process`` de produção lê amostras de log de
cada serviço e envia a um Loki com o ``loki.yaml`` de produção. Depois, cada consulta LogQL do
dashboard "Logs" roda contra esse Loki.
"""

import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from scripts.grafana_dashboards import LOKI, build

from tests.support.logs import (
    ALLOY_CONFIG,
    COMPOSE,
    DEPLOY,
    LOKI_CONFIG,
    alloy_blocks,
    counts,
    file_pipeline,
    image,
    label_values,
    loki_with_alloy,
    query,
)

SAMPLES = {
    "agent": (
        "agent.log",
        [
            '{"positions": 0, "event": "agent.started", "level": "info"}',
            '{"equity": "1000", "event": "risk.snapshot", "level": "debug"}',
            "Traceback (most recent call last):",
            '  File "/app/src/trade_agent/runtime.py", line 142, in run',
            '    await self.store.record_event("agent.stopped", Severity.INFO)',
            "ConnectionRefusedError: [Errno 111] Connect call failed",
            '{"error": "HTTP 404", "event": "universe.delist_schedule_unavailable", '
            '"level": "warning"}',
            '{"job": "check_risk", "event": "runtime.task_failed", "level": "error"}',
        ],
    ),
    "postgres": (
        "postgres.log",
        [
            "2026-09-30 17:04:00.000 UTC [1] LOG:  database system is ready to accept connections",
            "2026-09-30 17:05:00.000 UTC [596] FATAL:  terminating connection due to "
            "administrator command",
            "2026-09-30 17:05:01.000 UTC [30] WARNING:  there is no transaction in progress",
        ],
    ),
    "grafana": (
        "grafana.log",
        [
            "logger=provisioning t=2026-09-30T17:04:00Z level=error "
            'msg="Failed to read plugin provisioning files"',
            'logger=http.server t=2026-09-30T17:04:01Z level=info msg="HTTP Server Listen"',
        ],
    ),
    "docker-proxy": (
        "docker-proxy.log",
        ["[WARNING]  (1) : Server dockerbackend/dockersocket is UP"],
    ),
}
EXPECTED = {
    ("agent", "info"): 1,
    ("agent", "debug"): 1,
    ("agent", "error"): 2,  # traceback (uma entrada só) + runtime.task_failed
    ("agent", "warning"): 1,
    ("postgres", "info"): 1,
    ("postgres", "error"): 1,
    ("postgres", "warning"): 1,
    ("grafana", "error"): 1,
    ("grafana", "info"): 1,
    ("docker-proxy", None): 1,  # sem nível: aparece com o filtro de nível "All" (.*)
}
VARIABLES = {"$service": ".+", "$level": ".*", "$busca": "", "$__auto": "1m", "$__range": "1h"}


def _by_service_and_level(url: str) -> dict[tuple[str, str | None], int]:
    rows = counts(url, 'sum by (service, level) (count_over_time({service=~".+"}[1h]))')
    return {(dict(k)["service"], dict(k).get("level")): v for k, v in rows.items()}


@pytest.fixture(scope="module")
def loki(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    samples = tmp_path_factory.mktemp("logs") / "samples"
    samples.mkdir()
    for name, lines in SAMPLES.values():
        (samples / name).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    files = {service: name for service, (name, _) in SAMPLES.items()}
    for url in loki_with_alloy(samples, files):
        deadline = time.monotonic() + 60  # o multiline libera o último bloco em até 3 s
        while _by_service_and_level(url) != EXPECTED and time.monotonic() < deadline:
            time.sleep(1)
        yield url


def _loki_targets() -> list[Any]:
    return [
        pytest.param(target, panel["type"], id=panel["title"])
        for board in build().values()
        for panel in board["panels"]
        for target in panel["targets"]
        if target["datasource"] == LOKI
    ]


# ============================================================================ pipeline
def test_pipeline_labels_every_service_by_level(loki: str) -> None:
    assert _by_service_and_level(loki) == EXPECTED
    rows = counts(loki, 'sum by (event) (count_over_time({service="agent"} | event != "" [1h]))')
    events = {dict(k)["event"]: v for k, v in rows.items()}
    assert events == {
        "agent.started": 1,
        "risk.snapshot": 1,
        "universe.delist_schedule_unavailable": 1,
        "runtime.task_failed": 1,
    }


def test_python_traceback_becomes_one_error_entry(loki: str) -> None:
    (stream,) = query(loki, '{service="agent"} |~ "^Traceback"')
    assert stream["stream"]["level"] == "error"
    ((_, line),) = stream["values"]
    assert line.count("\n") == 3 and line.endswith("Connect call failed")


# ============================================================================ dashboard
@pytest.mark.parametrize(("target", "kind"), _loki_targets())
def test_dashboard_log_queries_run_on_loki(loki: str, target: dict[str, Any], kind: str) -> None:
    expr = target["expr"]
    for variable, value in VARIABLES.items():
        expr = expr.replace(variable, value)
    assert "$" not in expr
    result = query(loki, expr, instant=target["queryType"] == "instant")
    assert result  # as amostras alimentam todos os painéis
    if kind == "logs":
        assert {s["stream"]["service"] for s in result} <= set(SAMPLES)


def test_dashboard_variables_list_loki_labels(loki: str) -> None:
    board = build()["logs.json"]
    variables = {v["name"]: v for v in board["templating"]["list"]}
    assert set(variables) == {"service", "level", "busca"}
    for name in ("service", "level"):
        assert variables[name]["datasource"] == LOKI
        assert variables[name]["query"] == f"label_values({name})"
    assert set(label_values(loki, "service")) == set(SAMPLES)
    assert set(label_values(loki, "level")) == {"debug", "info", "warning", "error"}
    assert all(p["datasource"] == LOKI for p in board["panels"])


# ============================================================================ configuração
def test_loki_keeps_30_days_and_is_not_published() -> None:
    config = yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))
    assert config["limits_config"]["retention_period"] == "720h"
    assert config["compactor"]["retention_enabled"] is True
    assert config["auth_enabled"] is False  # por isso sem porta publicada
    (schema,) = config["schema_config"]["configs"]
    assert (schema["store"], schema["schema"]) == ("tsdb", "v13")
    loki = COMPOSE["services"]["loki"]
    assert "ports" not in loki
    assert any(v.endswith(":/loki") for v in loki["volumes"])


def test_docker_api_is_read_only_and_reachable_only_by_alloy() -> None:
    services = COMPOSE["services"]
    proxy = services["docker-proxy"]
    assert proxy["networks"] == ["docker-api"]
    assert COMPOSE["networks"]["docker-api"]["internal"] is True
    assert [n for n, s in services.items() if "docker-api" in s.get("networks", [])] == [
        "docker-proxy",
        "alloy",
    ]
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    enabled = {k for k, v in proxy["environment"].items() if v == "1"}
    assert enabled == {"CONTAINERS", "NETWORKS"}  # POST, EXEC etc. ficam no padrão (0)
    socket_users = [
        name
        for name, service in services.items()
        if any("docker.sock" in volume for volume in service.get("volumes", []))
    ]
    assert socket_users == ["docker-proxy"]


def test_alloy_config_matches_compose_and_loki() -> None:
    text = ALLOY_CONFIG.read_text(encoding="utf-8")
    hosts = re.findall(r'\bhost\s+=\s+"([^"]+)"', text)
    assert hosts == ["tcp://docker-proxy:2375"] * 2  # descoberta e leitura dos logs
    assert "unix:///var/run/docker.sock" not in text
    assert f"com.docker.compose.project={COMPOSE['name']}" in text
    port = yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))["server"]["http_listen_port"]
    assert f'url = "http://loki:{port}/loki/api/v1/push"' in alloy_blocks()["loki.write"]
    assert "loki.source.file" in file_pipeline({"agent": "agent.log"})
    for published in COMPOSE["services"]["alloy"]["ports"]:
        assert published.startswith("127.0.0.1:")


def test_every_service_rotates_its_logs() -> None:
    for name, service in COMPOSE["services"].items():
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "20m", "max-file": "5"},
        }, name


def test_grafana_datasource_points_to_the_loki_service() -> None:
    path = DEPLOY / "grafana" / "provisioning" / "datasources" / "loki.yaml"
    (datasource,) = yaml.safe_load(path.read_text(encoding="utf-8"))["datasources"]
    assert (datasource["uid"], datasource["type"]) == (LOKI["uid"], LOKI["type"])
    port = yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))["server"]["http_listen_port"]
    assert datasource["url"] == f"http://loki:{port}"
    assert (DEPLOY / "grafana" / "provisioning" / "plugins").is_dir()


def test_alloy_config_is_canonically_formatted() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível")
    done = subprocess.run(  # noqa: S603 - comando fixo, sem entrada externa
        ["docker", "run", "--rm", "-v", f"{ALLOY_CONFIG}:/etc/alloy/config.alloy:ro",  # noqa: S607
         image("alloy"), "fmt", "/etc/alloy/config.alloy"],
        capture_output=True, check=True,
    )  # fmt: skip
    assert done.stdout.decode("utf-8") == ALLOY_CONFIG.read_text(encoding="utf-8")


def test_versions_are_pinned() -> None:
    for name in ("loki", "alloy", "docker-proxy", "grafana"):
        assert re.fullmatch(r"[\w./-]+:v?\d+\.\d+\.\d+", image(name)), name
