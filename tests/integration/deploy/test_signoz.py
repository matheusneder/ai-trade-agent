"""SigNoz alongside Jaeger and Loki: Foundry manifests, local adjustments and collectors.

The full SigNoz (ClickHouse, keeper, metastore, migrations) is too heavy for the suite; the
configuration guarantees live here, checked on the model Docker Compose itself builds and
with the official binaries. Real ingestion was verified on the running stack.
"""

import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from tests.support.logs import COMPOSE, DEPLOY, compose_config, image

SIGNOZ = DEPLOY / "signoz"
POURS = SIGNOZ / "pours" / "deployment"
FOUNDRY = "signoz/foundryctl:v0.3.0"
CONTAINER_METRICS = DEPLOY / "otelcol" / "container-metrics.yaml"
EVERY_INTERFACE = "0.0.0.0"  # noqa: S104 - what the DEV_*_UI_HOST variables open, on purpose


def _docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível")


@pytest.fixture(scope="module")
def merged() -> dict[str, Any]:
    """The final compose model (include + override), as Docker Compose builds it."""
    return compose_config()


def test_every_published_port_is_local_and_unique(merged: dict[str, Any]) -> None:
    published = [
        (name, port)
        for name, service in merged["services"].items()
        for port in service.get("ports", [])
    ]
    assert {name for name, _ in published} >= {"signoz-signoz-0", "ingester", "grafana"}
    assert "jaeger" not in {name for name, _ in published}  # the UI goes out through jaeger-ui
    assert all(port["host_ip"] == "127.0.0.1" for _, port in published)
    counts = Counter(port["published"] for _, port in published)
    assert all(n == 1 for n in counts.values()), counts
    ports = {name: port["published"] for name, port in published if port["target"] in (4318, 8080)}
    # SigNoz's OTLP is on 14318 on the host, leaving 4318 (the OTLP default port) free
    assert (ports["ingester"], ports["signoz-signoz-0"]) == ("14318", "8080")


@pytest.mark.parametrize(
    ("variable", "opened"),
    [
        ("DEV_GRAFANA_UI_HOST", ("grafana", "3000")),
        ("DEV_SIGNOZ_UI_HOST", ("signoz-signoz-0", "8080")),
    ],
)
def test_only_the_requested_ui_opens_to_the_local_network(
    variable: str, opened: tuple[str, str]
) -> None:
    """Each DEV_*_UI_HOST (development) opens its UI alone; OTLP, PostgreSQL and the rest stay
    local."""
    merged = compose_config(**{variable: EVERY_INTERFACE})
    hosts = {
        (name, port["published"]): port["host_ip"]
        for name, service in merged["services"].items()
        for port in service.get("ports", [])
    }
    assert hosts.pop(opened) == EVERY_INTERFACE
    assert {("ingester", "14318"), ("postgres", "5432")} <= hosts.keys()
    assert set(hosts.values()) == {"127.0.0.1"}


def test_signoz_services_rotate_logs_and_pin_versions(merged: dict[str, Any]) -> None:
    signoz = {
        n: s for n, s in merged["services"].items() if n == "ingester" or n.startswith("signoz-")
    }
    assert len(signoz) == 7
    for name, service in signoz.items():
        assert service["logging"]["options"] == {"max-size": "20m", "max-file": "5"}, name
        assert not service["image"].endswith(":latest"), name
    casting = yaml.safe_load((SIGNOZ / "casting.yaml").read_text(encoding="utf-8"))["spec"]
    assert signoz["signoz-signoz-0"]["image"] == casting["signoz"]["spec"]["image"]
    assert signoz["ingester"]["image"] == casting["ingester"]["spec"]["image"]
    assert signoz["signoz-telemetrystore-migrator"]["image"] == casting["ingester"]["spec"]["image"]
    for spec in (casting["signoz"]["spec"], casting["ingester"]["spec"]):
        assert spec["image"].endswith(f":{spec['version']}")  # version and image consistent


def test_signoz_waits_for_its_metastore(merged: dict[str, Any]) -> None:
    """SigNoz exits at startup if the metadata PostgreSQL does not accept connections yet."""
    signoz = merged["services"]["signoz-signoz-0"]
    metastore = urlsplit(signoz["environment"]["SIGNOZ_SQLSTORE_POSTGRES_DSN"]).hostname
    assert metastore is not None and "healthcheck" in merged["services"][metastore]
    assert signoz["depends_on"][metastore]["condition"] == "service_healthy"


def test_clickhouse_keeps_its_hostname_across_recreations(merged: dict[str, Any]) -> None:
    """ClickHouse registers itself in the keeper under its hostname; the container ID, Docker's
    default, changes on every recreation and leaves stale entries that each startup resolves."""
    name = "signoz-telemetrystore-clickhouse-0-0"
    config = yaml.safe_load(
        (POURS / "telemetrystore" / "clickhouse" / "config-0-0.yaml").read_text(encoding="utf-8")
    )
    shards = config["remote_servers"]["cluster"]["shard"]
    assert [replica["host"] for shard in shards for replica in shard["replica"]] == [name]
    assert merged["services"][name]["hostname"] == name


def test_agent_and_collectors_reach_the_signoz_ingester(merged: dict[str, Any]) -> None:
    ingester = merged["services"]["ingester"]
    aliases = ingester["networks"]["signoz-network"]["aliases"]
    config = yaml.safe_load((POURS / "ingester" / "ingester.yaml").read_text(encoding="utf-8"))
    port = config["receivers"]["otlp"]["protocols"]["http"]["endpoint"].rsplit(":", 1)[1]
    target = f"http://{aliases[0]}:{port}"
    assert target == "http://signoz-ingester:4318"
    agent = merged["services"]["agent"]
    assert agent["environment"]["TA_OTLP_ENDPOINT"] == f"http://jaeger:4318,{target}"
    assert agent["environment"]["TA_OTLP_METRICS_ENDPOINT"] == target
    for name in ("agent", "alloy", "container-metrics"):
        assert "signoz-network" in merged["services"][name]["networks"], name
    collector = yaml.safe_load(CONTAINER_METRICS.read_text(encoding="utf-8"))
    assert collector["exporters"]["otlp_http"]["endpoint"] == target
    assert collector["receivers"]["docker_stats"]["endpoint"] == "tcp://docker-proxy:2375"
    assert COMPOSE["services"]["container-metrics"]["networks"] == ["docker-api", "signoz-network"]
    # SigNoz computes the LLM cost with the attributes the agent writes in the spans
    pricing = config["processors"]["signozllmpricing"]["attrs"]
    assert (
        pricing["model"] == "gen_ai.request.model" and pricing["in"] == "gen_ai.usage.input_tokens"
    )


def test_pours_are_in_sync_with_the_casting(tmp_path: Path) -> None:
    """The versioned manifests come from casting.yaml (as dashboards come from the generator)."""
    _docker()
    shutil.copy(SIGNOZ / "casting.yaml", tmp_path / "casting.yaml")
    subprocess.run(  # noqa: S603 - fixed command
        ["docker", "run", "--rm", "-v", f"{tmp_path}:/work", "-w", "/work", FOUNDRY,  # noqa: S607
         "forge", "--no-ledger", "--no-updater"],
        capture_output=True, check=True,
    )  # fmt: skip
    generated = {
        p.relative_to(tmp_path / "pours"): p for p in (tmp_path / "pours").rglob("*") if p.is_file()
    }
    versioned = {
        p.relative_to(SIGNOZ / "pours"): p for p in (SIGNOZ / "pours").rglob("*") if p.is_file()
    }
    assert sorted(generated) == sorted(versioned)
    for relative, path in generated.items():
        assert path.read_bytes() == versioned[relative].read_bytes(), relative


def test_container_metrics_config_is_valid() -> None:
    _docker()
    done = subprocess.run(  # noqa: S603 - fixed command
        ["docker", "run", "--rm", "-v", f"{CONTAINER_METRICS}:/etc/otelcol/config.yaml:ro",  # noqa: S607
         image("container-metrics"), "validate", "--config=/etc/otelcol/config.yaml"],
        capture_output=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
