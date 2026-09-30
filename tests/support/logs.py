"""Loki e Alloy em contêiner (testcontainers) com a configuração de produção de ``deploy/``.

O Alloy de teste usa o mesmo processamento (``loki.process``) e o mesmo envio (``loki.write``)
do arquivo versionado. Só a origem muda: arquivos de amostra no lugar da API do Docker.
"""

import contextlib
import re
import shutil
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

DEPLOY = Path(__file__).parents[2] / "deploy"
COMPOSE: dict[str, Any] = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text("utf-8"))
LOKI_CONFIG = DEPLOY / "loki" / "loki.yaml"
ALLOY_CONFIG = DEPLOY / "alloy" / "config.alloy"
TOP_LEVEL_BLOCK = re.compile(
    r'^(?P<name>[a-z_.]+)(?: "(?P<label>[^"]+)")? \{\n.*?^\}$', re.M | re.S
)


def image(service: str) -> str:
    return str(COMPOSE["services"][service]["image"])


def alloy_blocks() -> dict[str, str]:
    """Blocos de primeiro nível do ``config.alloy`` pelo identificador (``loki.write.loki``)."""
    text = ALLOY_CONFIG.read_text(encoding="utf-8")
    return {
        ".".join(filter(None, (m["name"], m["label"]))): m.group(0)
        for m in TOP_LEVEL_BLOCK.finditer(text)
    }


SIGNOZ_OTLP = "http://signoz-ingester:4318"
LOKI_OTLP = "http://loki:3100/otlp"
"""No teste, a cópia OTLP (a do SigNoz) vai para a entrada OTLP do próprio Loki."""


def file_pipeline(files: Mapping[str, str], *, otlp_endpoint: str = LOKI_OTLP) -> str:
    """Config do Alloy de teste: amostras por serviço → processamento e envio de produção."""
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
        "otelcol.receiver.loki.signoz",
        "otelcol.processor.transform.signoz",
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
    """Sobe Loki (alias ``loki``, como no compose) e Alloy lendo ``samples``; devolve a URL."""
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
    return list(body.get("data") or [])  # rótulo inexistente: sem "data"


def counts(url: str, expr: str) -> dict[tuple[tuple[str, str], ...], int]:
    """``sum by (...) (count_over_time(...))`` instantâneo como {rótulos: total}."""
    return {
        tuple(sorted(row["metric"].items())): int(row["value"][1])
        for row in query(url, expr, instant=True)
    }
