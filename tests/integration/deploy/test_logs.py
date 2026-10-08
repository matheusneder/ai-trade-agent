"""Container logs: Alloy → Loki → Grafana, with the configurations versioned in deploy/.

The pipeline runs for real: an Alloy with the production ``loki.process`` reads log samples
of each service and sends them to a Loki with the production ``loki.yaml``. Then every LogQL
query of the "Logs" dashboard runs against that Loki.
"""

import difflib
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
    PROJECT,
    SIGNOZ_OTLP,
    alloy_blocks,
    counts,
    file_pipeline,
    image,
    label_values,
    loki_with_alloy,
    query,
)

PROXY_TEMPLATE = DEPLOY / "docker-proxy" / "haproxy.cfg.template"
UPSTREAM_TEMPLATE = "/usr/local/etc/haproxy/haproxy.cfg.template"
RENDERED = "/tmp/haproxy.cfg"  # noqa: S108 - inside the container: the image generates the config there
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SAMPLES = {
    "agent": (
        "agent.log",
        [
            '{"positions": 0, "event": "agent.started", "level": "info"}',
            f'{{"equity": "1000", "event": "risk.snapshot", "level": "debug", '
            f'"trace_id": "{TRACE_ID}", "span_id": "b7ad6b7169203331"}}',
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
    "jaeger": (
        "jaeger.log",
        ['{"level":"warn","ts":"2026-09-30T18:10:12.207Z","msg":"aviso do coletor"}'],
    ),
    "container-metrics": (
        "container-metrics.log",
        [
            # a container that went away (docker run --rm, recreation): harmless, left out
            '{"level":"error","ts":"2026-10-01T17:05:41.120Z","caller":"docker@v0.161.0/'
            'docker.go:417","msg":"Could not inspect updated container","error":"Error '
            'response from daemon: No such container: 5f10dc58071"}',
            '{"level":"error","ts":"2026-10-01T17:06:02.300Z","msg":"Exporting failed. '
            'Dropping data.","error":"connection refused"}',
        ],
    ),
    "alloy": (
        "alloy.log",
        [
            'ts=2026-10-01T02:06:49.342Z level=warn msg="could not transfer logs" '
            "component_id=loki.source.docker.trade_agent component=tailer",
            # failure of the sending to SigNoz itself: goes only to Loki (see SIGNOZ_SELF)
            'ts=2026-10-01T11:45:59.974Z level=error msg="Exporting failed. Rejecting data." '
            'component_id=otelcol.exporter.otlphttp.signoz error="sending queue is full"',
        ],
    ),
}
EXPECTED = {
    ("agent", "info"): 1,
    ("agent", "debug"): 1,
    ("agent", "error"): 2,  # traceback (a single entry) + runtime.task_failed
    ("agent", "warning"): 1,
    ("postgres", "info"): 1,
    ("postgres", "error"): 1,
    ("postgres", "warning"): 1,
    ("grafana", "error"): 1,
    ("grafana", "info"): 1,
    ("docker-proxy", None): 1,  # no level: shows up with the "All" level filter (.*)
    ("jaeger", "warn"): 1,
    ("container-metrics", "error"): 1,  # only the real error; the vanished container's is dropped
    ("alloy", "warn"): 1,
    ("alloy", "error"): 1,
}
SIGNOZ_SELF = ("alloy", "error")
"""An Alloy error while sending to SigNoz does not go into the OTLP copy: each such line would
become one more send to the full queue, and the queue would never drain (millions per hour)."""
COPIES = {key: n for key, n in EXPECTED.items() if key != SIGNOZ_SELF}
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
        deadline = time.monotonic() + 60  # multiline releases the last block within 3 s
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


def test_trace_id_is_queryable_metadata(loki: str) -> None:
    (stream,) = query(loki, f'{{service="agent"}} | trace_id="{TRACE_ID}"')
    ((_, line),) = stream["values"]
    assert '"event": "risk.snapshot"' in line
    assert stream["stream"]["trace_id"] == TRACE_ID
    assert label_values(loki, "trace_id") == []  # metadata, not a label: no high cardinality


def test_otlp_copy_for_signoz_keeps_service_severity_and_trace(loki: str) -> None:
    """The OTLP copy (to SigNoz in production; to Loki's OTLP input in the test)."""
    copies = '{service_namespace="trade-agent"}'
    expr = f"sum by (service_name, severity_text) (count_over_time({copies}[1h]))"
    deadline = time.monotonic() + 30
    while sum(counts(loki, expr).values()) < sum(COPIES.values()):
        assert time.monotonic() < deadline
        time.sleep(1)
    time.sleep(2)  # anything else (the send's own error) would be here by now: batched
    by_severity = {
        (dict(k)["service_name"], dict(k).get("severity_text")): v
        for k, v in counts(loki, expr).items()
    }
    assert by_severity == COPIES  # same service and level as the Loki copy
    (stream,) = query(loki, f'{copies} | trace_id="{TRACE_ID}"')
    metadata = stream["stream"]
    assert (metadata["service_name"], metadata["span_id"]) == ("agent", "b7ad6b7169203331")
    assert (metadata["severity_number"], metadata["event"]) == ("5", "risk.snapshot")
    assert metadata["equity"] == "1000"  # JSON fields become searchable attributes
    assert "loki_attribute_labels" not in metadata
    (traceback,) = query(loki, f'{copies} |~ "^Traceback"')
    assert traceback["stream"]["severity_number"] == "17"  # ERROR


def test_vanished_container_noise_is_dropped(loki: str) -> None:
    """``docker_stats`` logs as an error every container that vanishes before being inspected
    (the lab creates and removes dozens per hour): noise, kept out of Loki and the SigNoz copy."""
    assert query(loki, '{service="container-metrics"} |= "No such container"') == []
    assert query(loki, '{service_name="container-metrics"} |= "No such container"') == []
    (kept,) = query(loki, '{service="container-metrics"}')
    assert "connection refused" in kept["values"][0][1]  # the real errors remain


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
    assert result  # the samples feed every panel
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
    assert set(label_values(loki, "level")) == {"debug", "info", "warning", "warn", "error"}
    assert all(p["datasource"] == LOKI for p in board["panels"])


# ============================================================================ configuration
def test_loki_keeps_30_days_and_is_not_published() -> None:
    config = yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))
    assert config["limits_config"]["retention_period"] == "720h"
    assert config["compactor"]["retention_enabled"] is True
    assert config["auth_enabled"] is False  # hence no published port
    (schema,) = config["schema_config"]["configs"]
    assert (schema["store"], schema["schema"]) == ("tsdb", "v13")
    loki = COMPOSE["services"]["loki"]
    assert "ports" not in loki
    assert any(v.endswith(":/loki") for v in loki["volumes"])


def test_docker_api_is_read_only_and_reachable_only_by_the_collectors() -> None:
    services = COMPOSE["services"]
    proxy = services["docker-proxy"]
    assert proxy["networks"] == ["docker-api"]
    assert COMPOSE["networks"]["docker-api"]["internal"] is True
    assert [n for n, s in services.items() if "docker-api" in s.get("networks", [])] == [
        "docker-proxy",
        "alloy",
        "container-metrics",
    ]
    assert proxy["volumes"] == [
        "/var/run/docker.sock:/var/run/docker.sock:ro",
        f"./docker-proxy/{PROXY_TEMPLATE.name}:{UPSTREAM_TEMPLATE}:ro",
    ]
    enabled = {k for k, v in proxy["environment"].items() if v == "1"}
    assert enabled == {"CONTAINERS", "NETWORKS"}  # POST, EXEC etc. stay at the default (0)
    socket_users = [
        name
        for name, service in services.items()
        if any("docker.sock" in volume for volume in service.get("volumes", []))
    ]
    assert socket_users == ["docker-proxy"]


def test_alloy_config_matches_compose_and_loki() -> None:
    text = ALLOY_CONFIG.read_text(encoding="utf-8")
    hosts = re.findall(r'\bhost\s+=\s+"([^"]+)"', text)
    assert hosts == ["tcp://docker-proxy:2375"] * 2  # discovery and reading of the logs
    assert "unix:///var/run/docker.sock" not in text
    assert f"com.docker.compose.project={PROJECT}" in text
    port = yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))["server"]["http_listen_port"]
    blocks = alloy_blocks()
    assert f'url = "http://loki:{port}/loki/api/v1/push"' in blocks["loki.write.loki"]
    assert f'endpoint = "{SIGNOZ_OTLP}"' in blocks["otelcol.exporter.otlphttp.signoz"]
    # the logs go to Loki and to SigNoz; SigNoz's internals are left out
    assert "loki.write.loki.receiver, loki.process.signoz.receiver" in text
    # to SigNoz, in batches (not one send per log line)
    chain = [
        ("loki.process.signoz", "otelcol.receiver.loki.signoz.receiver"),
        ("otelcol.receiver.loki.signoz", "otelcol.processor.transform.signoz.input"),
        ("otelcol.processor.transform.signoz", "otelcol.processor.batch.signoz.input"),
        ("otelcol.processor.batch.signoz", "otelcol.exporter.otlphttp.signoz.input"),
    ]
    for block, target in chain:
        assert target in blocks[block], block
    # the drop must be in the target list: in relabel_rules, the container would still be read,
    # with unlabeled logs (refused by Loki, accepted by SigNoz's OTLP copy)
    collected = blocks["discovery.relabel.collected"]
    assert "targets = discovery.docker.trade_agent.targets" in collected
    assert (
        "targets       = discovery.relabel.collected.output"
        in blocks["loki.source.docker.trade_agent"]
    )
    assert '"drop"' not in blocks["discovery.relabel.trade_agent"]
    drop = re.search(r'regex\s+=\s+"([^"]+)"\s+action\s+=\s+"drop"', collected)
    assert drop is not None
    for service in ("ingester", "signoz-telemetrystore-clickhouse-0-0", "signoz-signoz-0"):
        assert re.fullmatch(drop.group(1), service)
    for service in COMPOSE["services"]:
        assert not re.fullmatch(drop.group(1), service), service
    assert "loki.source.file" in file_pipeline({"agent": "agent.log"})
    for published in COMPOSE["services"]["alloy"]["ports"]:
        assert published.startswith("127.0.0.1:")


def test_read_positions_survive_docker_restarts() -> None:
    """Alloy keeps each container's read position by the labels of the discovered target.

    Network, IP and port change when Docker restarts. With those labels in the key, Alloy
    did not find the position and re-read each container's whole log: Loki refused the old
    lines, and the burst filled SigNoz's queue. Only labels stable for the container's life
    remain.
    """
    blocks = alloy_blocks()
    keep = re.search(
        r'regex\s+=\s+"([^"]+)"\s+action\s+=\s+"labelkeep"', blocks["discovery.relabel.collected"]
    )
    assert keep is not None
    needed = re.findall(
        r'source_labels\s+=\s+\["([^"]+)"\]', blocks["discovery.relabel.trade_agent"]
    )  # the labels that become "service"
    for label in ["__meta_docker_container_id", "__meta_docker_container_name", *needed]:
        assert re.fullmatch(keep.group(1), label), label
    volatile = [
        "__address__",
        "__meta_docker_network_id",
        "__meta_docker_network_ip",
        "__meta_docker_network_name",
        "__meta_docker_port_private",
        "__meta_docker_port_public",
        "__meta_docker_container_network_mode",
        "__meta_docker_container_label_com_docker_compose_version",
    ]
    for label in volatile:
        assert not re.fullmatch(keep.group(1), label), label


def test_docker_proxy_streams_logs_without_the_idle_cut() -> None:
    """The proxy template is the image's own plus a backend for ``docker logs --follow``.

    With the default (``timeout server 10m``), the log of a quiet container was cut every
    10 min; on reconnecting, Alloy re-read the lines of the last second read (duplicated in
    SigNoz).
    """
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível")
    ours = PROXY_TEMPLATE.read_text(encoding="utf-8")
    upstream = subprocess.run(  # noqa: S603 - fixed command, no external input
        ["docker", "run", "--rm", "--entrypoint", "cat", image("docker-proxy"),  # noqa: S607
         UPSTREAM_TEMPLATE],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")  # fmt: skip
    changes = [
        line
        for line in difflib.ndiff(upstream.splitlines(), ours.splitlines())
        if line.startswith(("+ ", "- "))
    ]
    added = [line[2:].strip() for line in changes if line.startswith("+ ")]
    assert len(added) == len(changes)  # nothing of the original removed or changed
    rule = (
        r"use_backend docker-logs if { path,url_dec -m reg -i "
        r"^(/v[\d\.]+)?/containers/[a-zA-Z0-9_.-]+/logs }"
    )
    assert [line for line in added if line and not line.startswith("#")] == [
        "backend docker-logs",
        "server dockersocket $SOCKET_PATH",
        "timeout server 24h",
        rule,
    ]
    logs = re.compile(rule.split(" -i ")[1].removesuffix(" }"))
    assert logs.search("/v1.47/containers/0b24ad795107/logs")
    assert logs.search("/containers/trade-agent-agent-1/logs")
    assert not logs.search("/v1.47/containers/json")
    assert not logs.search("/v1.47/containers/0b24ad795107/json")
    checked = subprocess.run(  # noqa: S603 - fixed command, no external input
        ["docker", "run", "--rm", "-v", f"{PROXY_TEMPLATE}:{UPSTREAM_TEMPLATE}:ro",  # noqa: S607
         image("docker-proxy"), "haproxy", "-c", "-f", RENDERED],
        capture_output=True, check=False,
    )  # fmt: skip
    output = (checked.stdout + checked.stderr).decode("utf-8")
    assert checked.returncode == 0, output
    assert "docker-logs" not in output  # no warning for the new backend (timeouts defined)


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
    done = subprocess.run(  # noqa: S603 - fixed command, no external input
        ["docker", "run", "--rm", "-v", f"{ALLOY_CONFIG}:/etc/alloy/config.alloy:ro",  # noqa: S607
         image("alloy"), "fmt", "/etc/alloy/config.alloy"],
        capture_output=True, check=True,
    )  # fmt: skip
    assert done.stdout.decode("utf-8") == ALLOY_CONFIG.read_text(encoding="utf-8")


def test_versions_are_pinned() -> None:
    for name in ("loki", "alloy", "docker-proxy", "grafana", "jaeger", "container-metrics"):
        assert re.fullmatch(r"[\w./-]+:v?\d+\.\d+\.\d+", image(name)), name
