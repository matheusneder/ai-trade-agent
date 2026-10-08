"""Loki and Alloy in containers (testcontainers) with the production configuration of ``deploy/``.

The test Alloy uses the same processing (``loki.process``) and the same sending
(``loki.write``) as the versioned file. Only the source changes: sample files instead of
the Docker API.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

DEPLOY = Path(__file__).parents[2] / "deploy"
COMPOSE: dict[str, Any] = yaml.safe_load((DEPLOY / "stack.yml").read_text("utf-8"))
PROJECT: str = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text("utf-8"))["name"]
LOKI_CONFIG = DEPLOY / "loki" / "loki.yaml"
ALLOY_CONFIG = DEPLOY / "alloy" / "config.alloy"
TOP_LEVEL_BLOCK = re.compile(
    r'^(?P<name>[a-z_.]+)(?: "(?P<label>[^"]+)")? \{\n.*?^\}$', re.M | re.S
)


def image(service: str) -> str:
    return str(COMPOSE["services"][service]["image"])


def compose_config(**env: str) -> dict[str, Any]:
    """The ``stack.yml`` model as Docker Compose builds it, with ``env`` in the environment.

    Without the root ``.env`` (``docker-compose.yml`` requires it) and without the ``DEV_``
    variables from the environment of whoever runs the tests: what counts is the default of
    a new environment.
    """
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível")
    base = {k: v for k, v in os.environ.items() if not k.startswith("DEV_")}
    done = subprocess.run(  # noqa: S603 - fixed command
        ["docker", "compose", "-f", str(DEPLOY / "stack.yml"), "config", "--format", "json"],  # noqa: S607
        capture_output=True, check=True, env=base | env,
    )  # fmt: skip
    model: dict[str, Any] = json.loads(done.stdout)
    return model


def alloy_blocks() -> dict[str, str]:
    """Top-level blocks of ``config.alloy`` by identifier (``loki.write.loki``)."""
    text = ALLOY_CONFIG.read_text(encoding="utf-8")
    return {
        ".".join(filter(None, (m["name"], m["label"]))): m.group(0)
        for m in TOP_LEVEL_BLOCK.finditer(text)
    }


SIGNOZ_OTLP = "http://signoz-ingester:4318"
LOKI_OTLP = "http://loki:3100/otlp"
"""In the test, the OTLP copy (SigNoz's) goes to Loki's own OTLP input."""


def file_pipeline(files: Mapping[str, str], *, otlp_endpoint: str = LOKI_OTLP) -> str:
    """Config of the test Alloy: samples per service → production processing and sending."""
    blocks = alloy_blocks()
    targets = ",\n".join(
        f'\t\t{{"__path__" = "/samples/{name}", "service" = "{service}"}}'
        for service, name in files.items()
    )
    source = (
        'loki.source.file "samples" {\n'
        f"\ttargets = [\n{targets},\n\t]\n"
        "\tforward_to = [loki.process.trade_agent.receiver]\n"
        "}"
    )
    exporter = blocks["otelcol.exporter.otlphttp.signoz"]
    assert f'endpoint = "{SIGNOZ_OTLP}"' in exporter
    names = [
        "loki.process.trade_agent",
        "loki.write.loki",
        "loki.process.signoz",
        "otelcol.receiver.loki.signoz",
        "otelcol.processor.transform.signoz",
        "otelcol.processor.batch.signoz",
    ]
    return (
        "\n\n".join(
            [*(blocks[n] for n in names), exporter.replace(SIGNOZ_OTLP, otlp_endpoint), source]
        )
        + "\n"
    )


def wait_ready(url: str, timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{url}/ready", timeout=2).text.strip() == "ready":
                return
        time.sleep(0.5)
    raise TimeoutError(f"Loki não ficou pronto em {timeout_s:.0f} s")


def loki_with_alloy(samples: Path, files: Mapping[str, str]) -> Iterator[str]:
    """Starts Loki (aliased ``loki``, as in compose) and Alloy on ``samples``; returns the URL."""
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível: testes de logs ignorados")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network

    config = samples.parent / "config.alloy"
    config.write_text(file_pipeline(files), encoding="utf-8", newline="\n")
    with Network() as network:
        loki = (
            DockerContainer(image("loki"))
            .with_volume_mapping(LOKI_CONFIG, "/etc/loki/loki.yaml")
            .with_command("-config.file=/etc/loki/loki.yaml")
            .with_exposed_ports(3100)
            .with_network(network)
            .with_network_aliases("loki")
        )
        with loki:
            url = f"http://{loki.get_container_host_ip()}:{loki.get_exposed_port(3100)}"
            wait_ready(url)
            alloy = (
                DockerContainer(image("alloy"))
                .with_volume_mapping(config, "/etc/alloy/config.alloy")
                .with_volume_mapping(samples, "/samples")
                .with_command("run --storage.path=/tmp/alloy /etc/alloy/config.alloy")
                .with_network(network)
            )
            with alloy:
                yield url


def query(url: str, expr: str, *, instant: bool = False, window_s: int = 3600) -> Any:
    now = time.time_ns()
    params: dict[str, Any] = {"query": expr, "limit": 1000}
    if instant:
        path, params["time"] = "query", now
    else:
        path = "query_range"
        params.update(start=now - window_s * 1_000_000_000, end=now, step=60)
    body = httpx.get(f"{url}/loki/api/v1/{path}", params=params, timeout=30).json()
    assert body["status"] == "success", body
    return body["data"]["result"]


def label_values(url: str, label: str) -> list[str]:
    body = httpx.get(f"{url}/loki/api/v1/label/{label}/values", timeout=10).json()
    assert body["status"] == "success", body
    return list(body.get("data") or [])  # label that does not exist: no "data"


def counts(url: str, expr: str) -> dict[tuple[tuple[str, str], ...], int]:
    """Instant ``sum by (...) (count_over_time(...))`` as {labels: total}."""
    return {
        tuple(sorted(row["metric"].items())): int(row["value"][1])
        for row in query(url, expr, instant=True)
    }
