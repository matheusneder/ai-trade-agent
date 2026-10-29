"""Ephemeral PostgreSQL + Loki + Grafana for the dashboard smoke tests (Docker Compose).

``tests/grafana-dashboard-smoke-tests/compose.yaml`` with the images of ``deploy/stack.yml``,
a unique project name, generated credentials and ports chosen by Docker on 127.0.0.1. The
real stack (project ``trade-agent``, ports 3000/5432) is never touched: every command here
carries ``-p <unique name>``.

The project name carries the PID of the pytest process that owns it
(``ta-grafana-smoke-<pid>-<random>``). A run killed before its teardown (a run stopped in
VS Code, a closed terminal) leaves its containers behind; the next run removes every smoke
stack whose owner process no longer exists, and never the stack of a run still going.

``GRAFANA_SMOKE_ATTACH`` names the JSON of a stack already running and seeded (:class:`Attached`,
written by the mutation tests, see tests/mutation/README.md): the session uses it instead of
starting its own, and provisions the generator's dashboards into it again.
"""

import asyncio
import contextlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import yaml
from scripts.grafana_dashboards import build, render
from sqlalchemy.ext.asyncio import create_async_engine

from trade_agent.persistence.migrate import upgrade_to_head

ROOT = Path(__file__).parents[2]
SUITE = ROOT / "tests" / "grafana-dashboard-smoke-tests"
COMPOSE_FILE = SUITE / "compose.yaml"
STACK = yaml.safe_load((ROOT / "deploy" / "stack.yml").read_text("utf-8"))
PROJECT_PREFIX = "ta-grafana-smoke-"
PROJECT = re.compile(rf"{PROJECT_PREFIX}(?P<pid>\d+)-[0-9a-f]+")
KEEP_ENV = "GRAFANA_SMOKE_KEEP"
"""``GRAFANA_SMOKE_KEEP=1`` leaves the stack running after the tests (debugging)."""
ATTACH_ENV = "GRAFANA_SMOKE_ATTACH"


@dataclass(frozen=True, slots=True)
class Stack:
    project: str
    grafana_url: str
    loki_url: str
    database_url: str
    """As the agent's owner user (``trade_agent``): migrations and seed."""
    admin_user: str
    admin_password: str
    dashboards: Path | None = None
    """The directory Grafana provisions the dashboards from; ``None``: the versioned JSON of
    ``deploy/grafana/dashboards``, as in production."""


@dataclass(frozen=True, slots=True)
class Attached:
    """A stack started and seeded once, for many sessions (``GRAFANA_SMOKE_ATTACH``)."""

    stack: Stack
    seed: int
    hours: int
    end: datetime
    """The seeded day's end: ``generate(seed, hours, end)`` gives the same rows again."""

    def save(self, path: Path) -> None:
        folder = self.stack.dashboards
        stack = asdict(self.stack) | {"dashboards": str(folder) if folder else None}
        data = {"stack": stack, "seed": self.seed, "hours": self.hours, "end": self.end}
        path.write_text(json.dumps(data, default=datetime.isoformat), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Attached":
        data = json.loads(path.read_text(encoding="utf-8"))
        folder = data["stack"]["dashboards"]
        stack = Stack(**data["stack"] | {"dashboards": Path(folder) if folder else None})
        return cls(stack, data["seed"], data["hours"], datetime.fromisoformat(data["end"]))


def attached() -> Attached | None:
    path = os.environ.get(ATTACH_ENV)
    return Attached.load(Path(path)) if path else None


def images() -> dict[str, str]:
    services = STACK["services"]
    return {
        "SMOKE_POSTGRES_IMAGE": services["postgres"]["image"],
        "SMOKE_LOKI_IMAGE": services["loki"]["image"],
        "SMOKE_GRAFANA_IMAGE": services["grafana"]["image"],
    }


def _compose(project: str, env: dict[str, str], *args: str, timeout: float = 600) -> str:
    done = subprocess.run(  # noqa: S603 - fixed command, project name generated here
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],  # noqa: S607
        capture_output=True, text=True, encoding="utf-8", env=os.environ | env,
        timeout=timeout, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{done.stderr}")
    return done.stdout


def _port(project: str, env: dict[str, str], service: str, port: int) -> str:
    address = _compose(project, env, "port", service, str(port)).strip().splitlines()[-1]
    return address.rsplit(":", 1)[-1]


def _wait(url: str, ok: Any, what: str, timeout_s: float = 180) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if ok(httpx.get(url, timeout=3)):
                return
        time.sleep(1)
    raise TimeoutError(f"{what} not ready in {timeout_s:.0f} s ({url})")


def run_async(coroutine: Coroutine[Any, Any, None]) -> None:
    """Runs a coroutine in its own thread: Playwright's sync API may own this thread's loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coroutine).result()


async def _migrate(url: str) -> None:
    engine = create_async_engine(url)
    try:
        await upgrade_to_head(engine)
    finally:
        await engine.dispose()


def logs(stack: Stack) -> str:
    env = _env(stack.admin_password, "-", "-", stack.dashboards)
    return _compose(stack.project, env, "logs", "--no-color", "--tail", "200")


def _env(admin: str, postgres: str, grafana_db: str, dashboards: Path | None) -> dict[str, str]:
    folder = {"SMOKE_GRAFANA_DASHBOARDS": str(dashboards)} if dashboards else {}
    return (
        images()
        | folder
        | {
            "SMOKE_GRAFANA_ADMIN_PASSWORD": admin,
            "SMOKE_POSTGRES_PASSWORD": postgres,
            "SMOKE_GRAFANA_DB_PASSWORD": grafana_db,
        }
    )


def write_dashboards(directory: Path) -> None:
    """The generator's dashboards (``build()``), as ``python -m scripts.grafana_dashboards``
    writes them, alone in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.iterdir():
        old.unlink()
    for name, board in build().items():
        (directory / name).write_text(render(board), encoding="utf-8", newline="\n")


def provision(stack: Stack) -> None:
    """Replaces every dashboard of the stack by the generator's, from a clean slate: an earlier
    session's dashboards (another UID, for one) never stay behind. With the folder empty, Grafana
    lets go of the provisioned dashboards (deletion is disabled in the provisioning: it keeps
    them, unprovisioned), the API deletes them, and Grafana reads the new files."""
    assert stack.dashboards is not None, "the versioned dashboards are provisioned at startup"
    auth = (stack.admin_user, stack.admin_password)
    with httpx.Client(base_url=stack.grafana_url, auth=auth, timeout=60) as api:
        for old in stack.dashboards.iterdir():
            old.unlink()
        api.post("/api/admin/provisioning/dashboards/reload").raise_for_status()
        found = api.get("/api/search", params={"type": "dash-db"}).raise_for_status().json()
        for board in found:
            api.delete(f"/api/dashboards/uid/{board['uid']}").raise_for_status()
        write_dashboards(stack.dashboards)
        api.post("/api/admin/provisioning/dashboards/reload").raise_for_status()


if sys.platform == "win32":
    import ctypes

    def _process_alive(pid: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, not ours
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

else:

    def _process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)  # signal 0: only checks that the process exists
        except ProcessLookupError:
            return False
        except PermissionError:  # it exists, owned by another user
            return True
        return True


def remove_stale_stacks() -> list[str]:
    """Removes the smoke stacks left by runs whose pytest process is gone."""
    listed = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",  # noqa: S607
         "--format", '{{.Label "com.docker.compose.project"}}'],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    ).stdout.split()  # fmt: skip
    stale = sorted(
        project
        for project in set(listed)
        if (match := PROJECT.fullmatch(project)) and not _process_alive(int(match["pid"]))
    )
    for project in stale:  # no -f: Compose finds the containers by the project label
        subprocess.run(  # noqa: S603 - project names matched above
            ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans",  # noqa: S607
             "--timeout", "5"],
            cwd=ROOT, capture_output=True, timeout=300, check=True,
        )  # fmt: skip
    return stale


def start(project: str, dashboards: Path | None = None) -> Stack:
    """Starts the stack and migrates the database (Alembic); removes it again if it does not
    get ready. ``dashboards``: a folder for the generator's dashboards (:func:`provision`)."""
    admin, postgres, grafana_db = (secrets.token_urlsafe(18) for _ in range(3))
    env = _env(admin, postgres, grafana_db, dashboards)
    if dashboards is not None:
        write_dashboards(dashboards)
    try:
        _compose(project, env, "up", "-d", "--quiet-pull")
        pg_port = _port(project, env, "postgres", 5432)
        loki = f"http://127.0.0.1:{_port(project, env, 'loki', 3100)}"
        grafana = f"http://127.0.0.1:{_port(project, env, 'grafana', 3000)}"
        url = f"postgresql+asyncpg://trade_agent:{postgres}@127.0.0.1:{pg_port}/trade_agent"
        run_async(_migrate(url))
        _wait(f"{loki}/ready", lambda r: r.text.strip() == "ready", "Loki")
        _wait(f"{grafana}/api/health", lambda r: r.json().get("database") == "ok", "Grafana")
    except BaseException:
        stop(Stack(project, "", "", "", "admin", admin, dashboards))
        raise
    return Stack(project, grafana, loki, url, "admin", admin, dashboards)


def stop(stack: Stack) -> None:
    """Removes the stack (``down -v``), unless ``GRAFANA_SMOKE_KEEP=1``."""
    if os.environ.get(KEEP_ENV) == "1":
        print(f"\n{KEEP_ENV}=1: {stack.project} kept until the next run (or `down -v` it)")
        return
    env = _env(stack.admin_password, "-", "-", stack.dashboards)
    _compose(stack.project, env, "down", "-v", "--remove-orphans", "--timeout", "5")


def ephemeral_stack() -> Iterator[Stack]:
    """The session's stack: the one of ``GRAFANA_SMOKE_ATTACH``, with the generator's dashboards
    provisioned again, or a new one (:func:`start`) removed at the end."""
    if (running := attached()) is not None:
        provision(running.stack)
        yield running.stack
        return
    if shutil.which("docker") is None:
        pytest.skip("Docker indisponível: smoke tests do Grafana ignorados")
    remove_stale_stacks()
    stack = start(f"{PROJECT_PREFIX}{os.getpid()}-{secrets.token_hex(4)}")
    try:
        yield stack
    finally:
        stop(stack)


def push_logs(loki_url: str, streams: list[dict[str, Any]], window_s: int = 1800) -> None:
    """Sends the synthetic logs to Loki (push API) in chronological 30-minute slices.

    Loki refuses an entry older than the newest one of its stream minus half of
    ``max_chunk_age`` (1 h by default), including the newest of the same request: a whole
    day in one request would be refused.
    """
    slices: dict[int, list[dict[str, Any]]] = {}
    for stream in streams:
        by_slice: dict[int, list[Any]] = {}
        for value in stream["values"]:
            by_slice.setdefault(int(value[0]) // (window_s * 10**9), []).append(value)
        for key, values in by_slice.items():
            slices.setdefault(key, []).append({"stream": stream["stream"], "values": values})
    for key in sorted(slices):
        response = httpx.post(
            f"{loki_url}/loki/api/v1/push",
            content=json.dumps({"streams": slices[key]}),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if response.status_code != 204:
            raise RuntimeError(f"Loki refused the logs: {response.status_code} {response.text}")
    # Queries older than ``query_ingesters_within`` (3 h) read only the storage: the lines of
    # the past hours are there only after the ingester flushes its chunks.
    httpx.post(f"{loki_url}/flush", timeout=30).raise_for_status()
    total = sum(len(stream["values"]) for stream in streams)
    oldest = min(int(v[0]) for stream in streams for v in stream["values"]) // 10**9
    hours = math.ceil((time.time() - oldest) / 3600) + 1
    query = f'sum(count_over_time({{service=~".+"}}[{hours}h]))'

    def stored(response: httpx.Response) -> bool:
        result = response.json()["data"]["result"]
        return bool(result) and int(result[0]["value"][1]) == total

    _wait(f"{loki_url}/loki/api/v1/query?{urlencode({'query': query})}", stored, "Loki flush")
