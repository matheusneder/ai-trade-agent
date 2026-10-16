"""The ephemeral SigNoz stack: Foundry's manifests from deploy/ plus compose.override.yaml.

Its own Compose project, network, volumes and random 127.0.0.1 ports; the real stack (project
``trade-agent``/``signoz``, ``signoz-*`` containers, ports 8080 and 14318) is never touched.
:meth:`Stack.up` renders the configuration first and refuses to start when anything still
points at a production name or port.
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
BASE = ROOT / "deploy" / "signoz" / "pours" / "deployment" / "compose.yaml"
OVERRIDE = HERE / "compose.override.yaml"
PROJECT = "ta-signoz-smoke"
# names of the real stack: the ephemeral configuration must not mention any of them
PRODUCTION_PROJECTS = {"signoz", "trade-agent"}
PRODUCTION_NETWORKS = {"signoz-network", "trade-agent_default", "trade-agent_docker-api"}
PRODUCTION_PREFIX = "signoz-"  # container and volume names of deploy/signoz


class StackError(RuntimeError):
    pass


def _run(*args: str, timeout: float = 600, check: bool = True) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed docker commands, no shell
            ["docker", *args],  # noqa: S607 - docker from the PATH, like the developer's
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StackError("docker not found: install Docker and keep it running") from exc
    if check and done.returncode != 0:
        raise StackError(f"docker {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def problems(config: dict[str, Any], project: str) -> list[str]:
    """What in a rendered ``docker compose config`` could reach the real stack."""
    found = []
    if config.get("name") in PRODUCTION_PROJECTS or config.get("name") != project:
        found.append(f"project name {config.get('name')!r}")
    for name, service in (config.get("services") or {}).items():
        container = service.get("container_name")
        if container and not container.startswith(f"{project}-"):
            found.append(f"service {name}: container_name {container!r}")
        for port in service.get("ports") or []:
            if port.get("host_ip") != "127.0.0.1" or port.get("published"):
                found.append(f"service {name}: port {port} is not a random 127.0.0.1 port")
        for mount in service.get("volumes") or []:
            source = str(mount.get("source", ""))
            if mount.get("type") == "volume" and source.startswith(PRODUCTION_PREFIX):
                found.append(f"service {name}: volume {source!r}")
    for key, network in (config.get("networks") or {}).items():
        name = network.get("name", key)
        if name in PRODUCTION_NETWORKS or network.get("external"):
            found.append(f"network {name!r}")
    for key, volume in (config.get("volumes") or {}).items():
        name = volume.get("name", key)
        if name.startswith(PRODUCTION_PREFIX) or volume.get("external"):
            found.append(f"volume {name!r}")
    return found


@dataclass(frozen=True, slots=True)
class Endpoints:
    ui: str  # SigNoz UI and API
    otlp: str  # ingester OTLP/HTTP


class Stack:
    def __init__(self, project: str = PROJECT) -> None:
        self.project = project

    def compose(self, *args: str, timeout: float = 600, check: bool = True) -> str:
        files = ["-f", str(BASE), "-f", str(OVERRIDE)]
        return _run("compose", "-p", self.project, *files, *args, timeout=timeout, check=check)

    def check_isolation(self) -> None:
        config = json.loads(self.compose("config", "--format", "json"))
        found = problems(config, self.project)
        if found:
            raise StackError(
                "refusing to start, it could touch the real stack: " + "; ".join(found)
            )

    def up(self, *, timeout: float = 600) -> Endpoints:
        """Starts from scratch (no leftovers of a previous run) and waits for it to be ready."""
        self.check_isolation()
        self.down()
        self.compose(
            "up", "-d", "--wait", "--wait-timeout", str(int(timeout)), timeout=timeout + 60
        )
        self.wait_migrations(timeout)
        endpoints = self.endpoints()
        _wait_http(f"{endpoints.ui}/api/v1/health", timeout)
        return endpoints

    def endpoints(self) -> Endpoints:
        return Endpoints(
            ui=f"http://{self.port('signoz-signoz-0', 8080)}",
            otlp=f"http://{self.port('ingester', 4318)}",
        )

    def port(self, service: str, container_port: int) -> str:
        """``127.0.0.1:<random port>`` published for ``service``."""
        published = self.compose("port", service, str(container_port)).strip()
        return published.replace("0.0.0.0", "127.0.0.1")  # noqa: S104 - only a rewrite

    def wait_migrations(self, timeout: float) -> None:
        """The ClickHouse schema (signoz-otel-collector migrate) must finish before the seed."""
        container = self.compose("ps", "-a", "-q", "signoz-telemetrystore-migrator").strip()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = _run("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", container)
            status, code = state.split()
            if status == "exited" and code == "0":
                return
            time.sleep(2)
        raise StackError("the ClickHouse migrations did not finish in time")

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", "--timeout", "5", timeout=300)

    def running(self) -> bool:
        return bool(self.compose("ps", "-q", check=False).strip())


def wait_otlp(endpoints: Endpoints, timeout: float = 180) -> None:
    """Waits for the ingester to accept data. It starts its receivers only after SigNoz sends it
    a configuration over OpAMP, and SigNoz does so only once an organization exists: call this
    after :func:`signoz_smoke.signoz.bootstrap` registered the first user."""
    _wait_http(f"{endpoints.otlp}/v1/traces", timeout, post=True)


def keep() -> bool:
    """``SIGNOZ_SMOKE_KEEP=1``: leaves the stack running after the tests (to look around)."""
    return os.environ.get("SIGNOZ_SMOKE_KEEP", "") not in {"", "0", "false", "no"}


def _wait_http(url: str, timeout: float, *, post: bool = False) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            if post:  # an empty OTLP request: 200 once the collector accepts data
                response = httpx.post(
                    url, content=b"", headers={"Content-Type": "application/x-protobuf"}
                )
            else:
                response = httpx.get(url)
            if response.status_code == 200:
                return
            last = f"HTTP {response.status_code}"
        except httpx.TransportError as exc:
            last = repr(exc)
        time.sleep(1)
    raise StackError(f"{url} not ready: {last}")
