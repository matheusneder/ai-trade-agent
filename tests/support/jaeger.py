"""Jaeger em contêiner (testcontainers) com a configuração de produção de ``deploy/jaeger``."""

import contextlib
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import yaml

from tests.support.logs import DEPLOY, image

JAEGER_CONFIG = DEPLOY / "jaeger" / "config.yaml"


def jaeger_config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(JAEGER_CONFIG.read_text(encoding="utf-8"))
    return config


def port(endpoint: str) -> int:
    return int(endpoint.rsplit(":", 1)[1])


@dataclass(frozen=True, slots=True)
class Jaeger:
    ui: str
    otlp: str

    def get(self, path: str, **params: Any) -> Any:
        response = httpx.get(f"{self.ui}{path}", params=params, timeout=10)
        response.raise_for_status()
        return response.json()


def jaeger_container() -> Iterator[Jaeger]:
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível: testes do Jaeger ignorados")
    from testcontainers.core.container import DockerContainer

    config = jaeger_config()
    ui_port = port(config["extensions"]["jaeger_query"]["http"]["endpoint"])
    otlp_port = port(config["receivers"]["otlp"]["protocols"]["http"]["endpoint"])
    health_port = port(config["extensions"]["healthcheckv2"]["http"]["endpoint"])
    container = (
        DockerContainer(image("jaeger"))
        .with_volume_mapping(JAEGER_CONFIG, "/etc/jaeger/config.yaml")
        .with_command("--config /etc/jaeger/config.yaml")
        .with_exposed_ports(ui_port, otlp_port, health_port)
    )
    with container:
        host = container.get_container_host_ip()
        health = f"http://{host}:{container.get_exposed_port(health_port)}/status"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                if httpx.get(health, timeout=2).json().get("healthy"):
                    break
            time.sleep(0.5)
        else:
            raise TimeoutError("Jaeger não ficou saudável em 60 s")
        yield Jaeger(
            ui=f"http://{host}:{container.get_exposed_port(ui_port)}",
            otlp=f"http://{host}:{container.get_exposed_port(otlp_port)}",
        )
