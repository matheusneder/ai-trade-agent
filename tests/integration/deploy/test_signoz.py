"""SigNoz em paralelo ao Jaeger e ao Loki: manifestos do Foundry, ajustes locais e coletores.

O SigNoz completo (ClickHouse, keeper, metastore, migrações) é pesado demais para a bateria;
aqui ficam as garantias de configuração, verificadas no modelo que o próprio Docker Compose
monta e com os binários oficiais. A ingestão real foi conferida na pilha em execução.
"""

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support.logs import COMPOSE, DEPLOY, image

SIGNOZ = DEPLOY / "signoz"
POURS = SIGNOZ / "pours" / "deployment"
FOUNDRY = "signoz/foundryctl:v0.3.0"
CONTAINER_METRICS = DEPLOY / "otelcol" / "container-metrics.yaml"


def _docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível")


@pytest.fixture(scope="module")
def merged() -> dict[str, Any]:
    """O modelo final do compose (include + override), como o Docker Compose o monta."""
    _docker()
    compose = str(DEPLOY / "docker-compose.yml")
    done = subprocess.run(  # noqa: S603 - comando fixo
        ["docker", "compose", "-f", compose, "config", "--format", "json"],  # noqa: S607
        capture_output=True, check=True,
    )  # fmt: skip
    model: dict[str, Any] = json.loads(done.stdout)
    return model


def test_every_published_port_is_local_and_unique(merged: dict[str, Any]) -> None:
    published = [
        (name, port)
        for name, service in merged["services"].items()
        for port in service.get("ports", [])
    ]
    assert {name for name, _ in published} >= {"signoz-signoz-0", "ingester", "jaeger", "grafana"}
    assert all(port["host_ip"] == "127.0.0.1" for _, port in published)
    counts = Counter(port["published"] for _, port in published)
    assert all(n == 1 for n in counts.values()), counts  # SigNoz na 14318, Jaeger na 4318
    ports = {name: port["published"] for name, port in published if port["target"] in (4318, 8080)}
    assert (ports["ingester"], ports["signoz-signoz-0"], ports["jaeger"]) == (
        "14318",
        "8080",
        "4318",
    )


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
        assert spec["image"].endswith(f":{spec['version']}")  # versão e imagem coerentes


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
    # o SigNoz calcula o custo do LLM com os atributos que o agente grava nos spans
    pricing = config["processors"]["signozllmpricing"]["attrs"]
    assert (
        pricing["model"] == "gen_ai.request.model" and pricing["in"] == "gen_ai.usage.input_tokens"
    )


def test_pours_are_in_sync_with_the_casting(tmp_path: Path) -> None:
    """Os manifestos versionados saem do casting.yaml (como os dashboards saem do gerador)."""
    _docker()
    shutil.copy(SIGNOZ / "casting.yaml", tmp_path / "casting.yaml")
    subprocess.run(  # noqa: S603 - comando fixo
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
    done = subprocess.run(  # noqa: S603 - comando fixo
        ["docker", "run", "--rm", "-v", f"{CONTAINER_METRICS}:/etc/otelcol/config.yaml:ro",  # noqa: S607
         image("container-metrics"), "validate", "--config=/etc/otelcol/config.yaml"],
        capture_output=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
