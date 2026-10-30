"""Mutation tests (mutmut) of the test suites; see tests/mutation/README.md.

    uv run python -m scripts.mutation run unit                # src/trade_agent vs tests/unit
    uv run python -m scripts.mutation run integration         # ... vs tests/integration
    uv run python -m scripts.mutation run smoke-grafana       # a dashboard generator vs its
    uv run python -m scripts.mutation run smoke-signoz        #   smoke tests (a sample)
    uv run python -m scripts.mutation report unit             # the last run's report again
    uv run python -m scripts.mutation mutmut unit browse      # mutmut itself (browse, show...)

mutmut forks a warm process per mutant, which Windows cannot do: on Windows and macOS (or with
``--docker``) each front runs in the image of ``tests/mutation/Dockerfile``, on a work copy of
the repository kept in the ``ta-mutation`` Docker volume; on Linux it runs here, on a work copy
in ``.mutation/work/<front>``. The work copy keeps mutmut's results: a new run only tests the
mutants of what changed (``--sample`` and the smoke fronts name their mutants, so those run
again). The reports go to ``.mutation/<front>/`` (``report.md`` and ``results.json``).

Only ephemeral containers are started (a PostgreSQL server for the integration front, a stack
per smoke front), with ``ta-mutation-`` names; the real stack is never touched.
"""

import argparse
import hashlib
import json
import math
import os
import random
import secrets
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ta-mutation-runner"
VOLUME = "ta-mutation"
PREFIX = "ta-mutation-"
OUT_ENV = "TA_MUTATION_OUT"
EFFECTS = ".mutation-effects"  # in the work copy: what each smoke mutant changed
SMOKE = ".smoke"  # in the work copy: the attached stack's files

# mutmut's exit codes (mutmut.stats.status_by_exit_code); anything else is "suspicious"
STATUS = {
    1: "killed", 3: "killed", 0: "survived", 5: "no tests", 33: "no tests", 2: "interrupted",
    None: "not checked", 34: "skipped", 35: "suspicious", 36: "timeout", 24: "timeout",
    -24: "timeout", 152: "timeout", 255: "timeout", 37: "caught by type check",
    -11: "segfault", -9: "segfault",
}  # fmt: skip
KILLED = {"killed", "timeout", "segfault", "caught by type check"}


@dataclass(frozen=True, slots=True)
class Front:
    """A test suite and the code it is held to: what mutmut mutates and which tests it runs."""

    tests: str
    source: str
    """mutmut's ``source_paths``: copied into its work tree, mutated where ``mutate`` matches."""
    mutate: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    """Test files left out: they run the agent's code in another container (Freqtrade), where
    the mutated code cannot run (it imports mutmut) and no mutant can be switched on."""
    smoke: str | None = None
    """``grafana``/``signoz``: one stack for every session, the plugin of tests/support/mutation.py,
    one mutant at a time and a sample of the mutants that change a dashboard."""
    sample: int | None = None
    help: str = ""


FRONTS = {
    "unit": Front(
        tests="tests/unit",
        source="src",
        mutate=("src/trade_agent/*",),
        exclude=("src/trade_agent/persistence/migrations/*",),
        help="src/trade_agent (without the migrations) against tests/unit",
    ),
    "integration": Front(
        tests="tests/integration",
        source="src",
        mutate=("src/trade_agent/*",),
        exclude=("src/trade_agent/persistence/migrations/*",),
        ignore=(
            "tests/integration/lab/test_lab_causality.py",
            "tests/integration/lab/test_lab_families.py",
        ),
        help="src/trade_agent (without the migrations) against tests/integration",
    ),
    "smoke-grafana": Front(
        tests="tests/grafana-dashboard-smoke-tests",
        source="scripts",
        mutate=("scripts/grafana_dashboards.py",),
        smoke="grafana",
        sample=150,
        help="scripts/grafana_dashboards.py against tests/grafana-dashboard-smoke-tests",
    ),
    "smoke-signoz": Front(
        tests="tests/signoz-dashboard-smoke-tests",
        source="scripts",
        mutate=("scripts/signoz_dashboards.py",),
        smoke="signoz",
        sample=150,
        help="scripts/signoz_dashboards.py against tests/signoz-dashboard-smoke-tests",
    ),
}
# what the smoke fronts' fork server imports before forking a worker: libraries only, so each
# worker imports the tests and the generator itself, with its mutant already active (the panels
# the tests expect come from build() at import). No pytest plugin (pytest_playwright...): imported
# before pytest, it cannot have its asserts rewritten, and that warning is an error here
PRELOAD = (
    "httpx", "numpy", "pandas", "playwright.sync_api", "pydantic", "pydantic_settings",
    "sqlalchemy", "sqlalchemy.ext.asyncio", "asyncpg", "yaml",
    "opentelemetry.sdk.trace", "structlog",
)  # fmt: skip


# ============================================================== shell helpers
def _run(*args: str, cwd: Path | None = None, check: bool = True, **kw: Any) -> str:
    done = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False, **kw
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}... failed ({done.returncode}):\n{done.stderr}")
    return str(done.stdout)


def _say(message: str) -> None:
    print(f"[mutation {datetime.now(UTC):%H:%M:%S}] {message}", flush=True)


# ============================================================== host side
def _needs_docker(options: argparse.Namespace) -> bool:
    if options.docker or options.local:
        return bool(options.docker)
    return not (sys.platform == "linux" and hasattr(os, "fork"))


def _image() -> str:
    """The runner image, rebuilt when its Dockerfile or the locked dependencies change."""
    digest = hashlib.sha256()
    for name in ("tests/mutation/Dockerfile", "uv.lock"):
        digest.update((ROOT / name).read_bytes())
    tag = f"{IMAGE}:{digest.hexdigest()[:12]}"
    found = subprocess.run(["docker", "image", "inspect", tag], capture_output=True, check=False)
    if found.returncode:
        _say(f"building {tag} (a few minutes, once per dependency change)")
        subprocess.run(
            ["docker", "build", "-f", "tests/mutation/Dockerfile", "-t", tag, "."],
            cwd=ROOT, check=True,
        )  # fmt: skip
    return tag


def _manifest() -> list[str]:
    """The repository's files as git sees them: tracked or new, not ignored (uncommitted
    changes included)."""
    listed = _run("git", "ls-files", "-co", "--exclude-standard", "-z", cwd=ROOT)
    return sorted(name for name in listed.split("\0") if name and (ROOT / name).is_file())


def _git_dir() -> str:
    """The repository's git directory (the main one, for a worktree): the image has no other."""
    found = _run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=ROOT)
    return str(Path(found.strip()))


def _commit() -> str:
    head = _run("git", "rev-parse", "--short", "HEAD", cwd=ROOT).strip()
    dirty = _run("git", "status", "--porcelain", cwd=ROOT).strip()
    return f"{head}{' + uncommitted changes' if dirty else ''}"


def host(options: argparse.Namespace, argv: Sequence[str]) -> int:
    """Runs the command in the front's work copy: in the runner image, or here (Linux)."""
    out = ROOT / ".mutation"
    out.mkdir(exist_ok=True)
    (out / "manifest.txt").write_text("\n".join(_manifest()), encoding="utf-8")
    env = {"TA_MUTATION_COMMIT": _commit()}
    inner = [a for a in argv if a not in {"--docker", "--local"}]
    if not _needs_docker(options):
        work = out / "work" / options.front
        sync(ROOT, work, out / "manifest.txt")
        env |= {OUT_ENV: str(out)}
        command = [sys.executable, "-m", "scripts.mutation", *inner, "--here"]
        return subprocess.run(command, cwd=work, env=os.environ | env, check=False).returncode
    image = _image()
    _run("docker", "volume", "create", VOLUME)
    mount = _run("docker", "volume", "inspect", "-f", "{{.Mountpoint}}", VOLUME).strip()
    work = f"{mount}/{options.front}"
    name = f"{PREFIX}{options.front}"
    if _run("docker", "ps", "-aq", "--filter", f"name=^{name}$").strip():
        raise SystemExit(f"{name} is running already (docker rm -f {name} to stop it)")
    env |= {
        OUT_ENV: "/out",
        "GIT_DIR": "/repo-git",  # the legacy regression test reads its base commit
        "TMPDIR": f"{mount}/tmp/{options.front}",  # tmp_path mounted into sibling containers
        "TESTCONTAINERS_HOST_OVERRIDE": "127.0.0.1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = (
        f"python /repo/scripts/mutation.py sync /repo {work} /out/manifest.txt"
        f" && mkdir -p {env['TMPDIR']} && cd {work}"
        f" && exec python -m scripts.mutation {' '.join(_quote(a) for a in inner)} --here"
    )
    command = [
        "docker", "run", "--rm", "--name", name, "--network", "host",
        "-v", f"{ROOT}:/repo:ro", "-v", f"{_git_dir()}:/repo-git:ro",
        "-v", f"{VOLUME}:{mount}", "-v", f"{out}:/out",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        *(arg for key, value in env.items() for arg in ("-e", f"{key}={value}")),
        *(["-it"] if options.command == "mutmut" and sys.stdin.isatty() else []),
        image, "sh", "-c", script,
    ]  # fmt: skip
    return subprocess.run(command, check=False).returncode


def _quote(arg: str) -> str:
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def sync(source: Path, work: Path, manifest: Path) -> None:
    """Copies the repository's files into the work copy (only the ones that changed) and removes
    the ones it no longer has; mutmut's tree (``mutants/``) and the runner's files stay."""
    names = [n for n in manifest.read_text(encoding="utf-8").split("\n") if n]
    work.mkdir(parents=True, exist_ok=True)
    previous = work / ".manifest"
    old = set(previous.read_text(encoding="utf-8").split("\n")) if previous.exists() else set()
    for name in names:
        src, dst = source / name, work / name
        if not src.is_file():
            continue
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for name in old - set(names) - {""}:
        (work / name).unlink(missing_ok=True)
    previous.write_text("\n".join(names), encoding="utf-8")


# ============================================================== work copy: setup
def _configure(front: Front, *, debug: bool = False) -> None:
    """Appends the front's ``[tool.mutmut]`` to the work copy's pyproject.toml. mutmut runs the
    tests in its own tree (``mutants/``): everything the repository has goes there too."""
    names = Path(".manifest").read_text(encoding="utf-8").split("\n")
    copied = sorted(
        {name.split("/")[0] for name in names if name}
        - {front.source, "tests", "pyproject.toml", "uv.lock"}
    )
    config: dict[str, Any] = {
        "source_paths": [front.source],
        "only_mutate": list(front.mutate),
        "do_not_mutate": list(front.exclude),
        "pytest_add_cli_args_test_selection": [front.tests],
        "pytest_add_cli_args": ["-p", "no:cacheprovider", *(f"--ignore={i}" for i in front.ignore)],
        "also_copy": copied,
        "process_isolation": "forkserver",
        "use_git_change_detection": False,
        "debug": debug,
    }
    if front.smoke:
        Path(".mutation-preload.txt").write_text("\n".join(PRELOAD), encoding="utf-8")
        config |= {
            "pytest_add_cli_args": ["-p", "no:cacheprovider", "-p", "tests.support.mutation"],
            "forkserver_warmup": "import",
            "preload_modules_file": ".mutation-preload.txt",
        }
    pyproject = Path("pyproject.toml")
    text = pyproject.read_text(encoding="utf-8").split("\n[tool.mutmut]\n")[0]
    lines = [f"{key} = {json.dumps(value)}" for key, value in config.items()]  # JSON: valid TOML
    pyproject.write_text(text + "\n[tool.mutmut]\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _install_project() -> None:
    """The project's metadata (the tracing reads its version): an editable install of the work
    copy, unless one is there already. mutmut takes the original ``src`` off the path."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        version("trade-agent")
    except PackageNotFoundError:
        _run("uv", "pip", "install", "--python", sys.executable, "--no-deps", "-e", ".")
        sys.path.append(str(Path("src").resolve()))  # this process read the .pth files already


def _remove_leftovers(front_name: str) -> None:
    """What a killed run of this front left behind: its PostgreSQL server and stacks (names
    and labels of this script only, never another container)."""
    if shutil.which("docker") is None:
        return
    label = f"ta.mutation.front={front_name}"
    for container in _run("docker", "ps", "-aq", "--filter", f"label={label}").split():
        _run("docker", "rm", "-f", "-v", container, check=False)
    prefix = f"{PREFIX}{front_name.removeprefix('smoke-')}"
    listed = _run(
        "docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
        "--format", '{{.Label "com.docker.compose.project"}}',
    ).split()  # fmt: skip
    for project in sorted({p for p in listed if p.startswith(prefix)}):
        _run("docker", "compose", "-p", project, "down", "-v", "--remove-orphans", check=False)


@dataclass(slots=True)
class Postgres:
    """An ephemeral PostgreSQL server for the integration front: a database per session
    (``TA_TEST_POSTGRES_SERVER``, tests/support/database.py) instead of a container each."""

    front: str
    container: str = ""

    def start(self) -> dict[str, str]:
        from tests.support.logs import image

        password = secrets.token_urlsafe(18)
        self.container = f"{PREFIX}{self.front}-postgres-{secrets.token_hex(4)}"
        _run(
            "docker", "run", "-d", "--rm", "--name", self.container,
            "--label", f"ta.mutation.front={self.front}", "-p", "127.0.0.1::5432",
            "--tmpfs", "/var/lib/postgresql", "-e", f"POSTGRES_PASSWORD={password}",
            image("postgres"), "-c", "max_connections=500", "-c", "fsync=off",
            "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
        )  # fmt: skip
        port = _run("docker", "port", self.container, "5432").split()[0].rsplit(":", 1)[1]
        # over TCP: the entrypoint's first server (initdb) listens only on the socket
        ready = ("docker", "exec", self.container, "pg_isready", "-h", "127.0.0.1")
        deadline = time.monotonic() + 120
        while "accepting" not in _run(*ready, check=False):
            if time.monotonic() > deadline:
                raise TimeoutError("the PostgreSQL server did not start")
            time.sleep(1)
        url = f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/postgres"
        return {"TA_TEST_POSTGRES_SERVER": url}

    def stop(self) -> None:
        if self.container:
            _run("docker", "rm", "-f", "-v", self.container, check=False)


@dataclass(slots=True)
class SmokeStack:
    """One smoke stack for every pytest session of a front, seeded once, restarted when the
    seed gets old (the dashboards read "today"; a new UTC day breaks the clean run)."""

    kind: str
    max_age_s: float
    started: float = 0.0
    day: str = ""
    stop_stack: Any = None

    def attach_file(self) -> Path:
        return Path(SMOKE).resolve() / f"{self.kind}.json"

    def env(self) -> dict[str, str]:
        name = "GRAFANA_SMOKE_ATTACH" if self.kind == "grafana" else "SIGNOZ_SMOKE_ATTACH"
        return {name: str(self.attach_file())}

    def stale(self) -> bool:
        return (
            time.monotonic() - self.started > self.max_age_s
            or datetime.now(UTC).date().isoformat() != self.day
        )

    def start(self) -> None:
        self.stop()
        Path(SMOKE).mkdir(exist_ok=True)
        _say(f"starting the {self.kind} stack and its seed")
        (self._grafana if self.kind == "grafana" else self._signoz)()
        self.started, self.day = time.monotonic(), datetime.now(UTC).date().isoformat()

    def _grafana(self) -> None:
        from tests.support.grafana_fixtures import load_day
        from tests.support.grafana_seed import generate
        from tests.support.grafana_stack import Attached, start, stop

        project = f"{PREFIX}grafana-{secrets.token_hex(4)}"
        stack = start(project, Path(SMOKE).resolve() / "grafana-dashboards")
        self.stop_stack = lambda: stop(stack)
        seed, hours = int(os.environ.get("GRAFANA_SMOKE_SEED", "42")), 24
        data = generate(seed=seed, hours=hours)
        load_day(stack, data)
        Attached(stack, seed, hours, data.end).save(self.attach_file())

    def _signoz(self) -> None:
        from tests.support.signoz_smoke import environment, signoz
        from tests.support.signoz_smoke.fixtures import Attached, load_day
        from tests.support.signoz_smoke.seed import default_end, generate

        stack = environment.Stack()
        endpoints = stack.up()
        self.stop_stack = stack.down
        credentials = signoz.bootstrap(endpoints.ui)
        seed, hours, end = int(os.environ.get("SIGNOZ_SMOKE_SEED", "7")), 24.0, default_end()
        data = generate(seed, hours, end)
        with signoz.api(endpoints.ui, credentials) as http:
            signoz.provision(http)
            load_day(stack, endpoints, http, data)
        Attached(stack.project, endpoints, credentials, seed, hours, end).save(self.attach_file())

    def stop(self) -> None:
        if self.stop_stack is not None:
            self.stop_stack()
            self.stop_stack = None


def _rename_signoz_project() -> None:
    """The work copy's SigNoz stack gets its own names (the suite's are fixed in its override):
    it never meets the stack of a smoke run going on at the same time."""
    for name in (
        "tests/signoz-dashboard-smoke-tests/compose.override.yaml",
        "tests/support/signoz_smoke/environment.py",
    ):
        path = Path(name)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("ta-signoz-smoke", f"{PREFIX}signoz"), encoding="utf-8")


# ============================================================== work copy: mutants
GENERATE = """
import sys
from mutmut.__main__ import create_mutants
from mutmut.mutation.trampoline import set_mutant_under_test
from mutmut.utils.file_utils import copy_also_copy_files, copy_src_dir
set_mutant_under_test("mutant_generation")
copy_src_dir()
copy_also_copy_files()
create_mutants(int(sys.argv[1]))
"""
# the static pass of the smoke fronts: what each mutant does to the generator's dashboards
EFFECT = """
import importlib, json, os, sys
sys.path.insert(0, os.path.abspath("mutants"))
module = importlib.import_module(sys.argv[1])
key = sys.argv[2]
def boards():
    built = module.build().values()
    return {str(b[key]): json.dumps(b, sort_keys=True, default=str) for b in built}
os.environ["MUTANT_UNDER_TEST"] = ""
clean = boards()
result = {}
for name in json.loads(sys.stdin.read()):
    os.environ["MUTANT_UNDER_TEST"] = name
    try:
        mutated = boards()
    except Exception as error:
        result[name] = {"error": f"{type(error).__name__}: {error}"}
        continue
    changed = (set(clean) ^ set(mutated)) | {k for k in clean if mutated.get(k) != clean[k]}
    result[name] = {"changed": sorted(changed)}
print(json.dumps(result))
"""
DIFFS = """
import json, sys
from mutmut.mutation.diff_apply import get_diff_for_mutant
print(json.dumps({n: get_diff_for_mutant(n, path=p) for n, p in json.loads(sys.stdin.read())}))
"""


def mutants() -> dict[str, str]:
    """Every mutant of the work tree (``mutants/``) → the source file it mutates."""
    found = {}
    for meta in Path("mutants").rglob("*.py.meta"):
        source = meta.relative_to("mutants").as_posix().removesuffix(".meta")
        for name in json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"]:
            found[name] = source
    return found


def statuses() -> dict[str, str]:
    found = {}
    for meta in Path("mutants").rglob("*.py.meta"):
        for name, code in json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"].items():
            found[name] = STATUS.get(code, "suspicious")
    return found


def _python(code: str, *args: str, stdin: str = "") -> str:
    return _run(sys.executable, "-c", code, *args, input=stdin)


def _mutmut(*args: str, env: dict[str, str]) -> int:
    command = [sys.executable, "-m", "mutmut", *args]
    return subprocess.run(command, env=os.environ | env, check=False).returncode


def _function_of(mutant: str) -> str:
    return mutant.partition("__mutmut_")[0]


def _children(options: argparse.Namespace, front: Front) -> int:
    if front.smoke:
        return 1  # one stack: a mutant's dashboards at a time
    return options.max_children or max(1, min(8, (os.cpu_count() or 4) - 2))


PLAN = Path(".mutation-plan.json")  # in the work copy: the last run's plan, for `report`


@dataclass(slots=True)
class Plan:
    """The mutants a run tests and, for the smoke fronts, what the static pass decided."""

    names: list[str] = field(default_factory=list)
    """Named mutants (a sample or a smoke front); empty: every mutant mutmut has not tested."""
    effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    seed: int = 0
    flaky: list[str] = field(default_factory=list)
    """Smoke fronts: killed once, survived when run again in another session."""
    unconfirmed: list[str] = field(default_factory=list)
    """Smoke fronts: killed once, not run again (that batch's clean run failed twice)."""

    def save(self) -> None:
        data = {"names": self.names, "effects": self.effects, "seed": self.seed}
        data |= {"flaky": self.flaky, "unconfirmed": self.unconfirmed}
        PLAN.write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls) -> "Plan":
        return cls(**json.loads(PLAN.read_text(encoding="utf-8"))) if PLAN.exists() else cls()


def plan(front: Front, options: argparse.Namespace) -> Plan:
    """Every mutant (a front's first run, then the ones of what changed), a random sample of
    them (``--sample``), or for a smoke front a sample of the ones the static pass leaves."""
    all_mutants = sorted(mutants())
    result = Plan(seed=options.seed)
    rng = random.Random(options.seed)
    size = options.sample if options.sample is not None else front.sample
    pool = all_mutants
    if front.smoke:
        result.effects = _effects(front, all_mutants)
        pool = [name for name, effect in result.effects.items() if effect["kind"] == "candidate"]
        size = size or len(pool)
    if size:
        result.names = sorted(rng.sample(pool, min(size, len(pool))))
        _forget(set(result.names))
    result.save()
    return result


def _forget(names: set[str]) -> None:
    """Marks the named mutants "not checked": a run that stops early does not report the
    results of an earlier one as its own."""
    for meta in Path("mutants").rglob("*.py.meta"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        codes = data["exit_code_by_key"]
        if names & codes.keys():
            codes.update(dict.fromkeys(names & codes.keys()))
            meta.write_text(json.dumps(data), encoding="utf-8")


def _effects(front: Front, names: list[str]) -> dict[str, dict[str, Any]]:
    """Static pass over every mutant of the generator, without the stack: ``crash`` (``build()``
    raises: every test fails at import), ``no effect`` (same dashboards, and the tests do not call
    the function directly) or ``candidate`` (the smoke tests decide)."""
    sys.path.insert(0, str(Path.cwd()))
    from tests.support import mutation

    suite = next(s for s in mutation.SUITES if s.folder in front.tests)
    direct = mutation.called_directly(suite)
    found = json.loads(_python(EFFECT, suite.generator, suite.key, stdin=json.dumps(names)))
    for name, effect in found.items():
        function = mutation._function(name)[1]
        if effect.get("error"):
            effect["kind"] = "crash"
        elif not effect["changed"] and function not in direct:
            effect["kind"] = "no effect"
        else:
            effect["kind"] = "candidate"
    return dict(found)


# ============================================================== run
def execute(front_name: str, options: argparse.Namespace) -> int:
    front = FRONTS[front_name]
    started = time.monotonic()
    children = _children(options, front)
    _configure(front, debug=options.debug)
    _install_project()
    _remove_leftovers(front_name)
    if front.smoke == "signoz":
        _rename_signoz_project()
    shutil.rmtree(EFFECTS, ignore_errors=True)
    _say("generating the mutants")
    _python(GENERATE, str(children))
    work = plan(front, options)
    count = f"{len(mutants())} mutants"
    _say(count + (f"; testing {len(work.names)} (seed {work.seed})" if work.names else ""))
    env: dict[str, str] = {}
    services: list[Postgres | SmokeStack] = []
    try:
        if front_name == "integration":
            services.append(server := Postgres(front_name))
            env |= server.start()
        if front.smoke:
            services.append(stack := SmokeStack(front.smoke, options.max_age * 60))
            env |= stack.env() | {"TA_MUTATION_EFFECTS": str(Path(EFFECTS).resolve())}
            code = _run_smoke(stack, work, options.batch, env)
        else:
            code = _mutmut("run", "--max-children", str(children), *work.names, env=env)
    finally:
        for service in services:
            service.stop()
    report(front_name, work, time.monotonic() - started)
    return code


def _run_smoke(stack: SmokeStack, work: Plan, batch: int, env: dict[str, str]) -> int:
    """The sample, then its kills again: a kill counts only if the mutant fails in a second
    session too. A smoke test can fail for a reason of its own (Grafana sometimes stalls for
    half a minute under load), and such failures come in bursts: the second run, later, tells."""
    skipped = _batches(stack, work.names, batch, env)
    first = statuses()
    kills = [n for n in work.names if first.get(n) in KILLED and n not in skipped]
    _say(f"confirming the {len(kills)} kills: each must fail again, in another session")
    work.unconfirmed = sorted(_batches(stack, kills, batch, env))
    again = statuses()
    work.flaky = sorted(n for n in kills if again.get(n) not in KILLED)
    work.save()
    return 1 if skipped else 0


def _batches(stack: SmokeStack, names: list[str], batch: int, env: dict[str, str]) -> list[str]:
    """The named mutants in batches; the stack restarts between them when its seed got old, and a
    batch that crossed a UTC midnight (or whose clean run failed) runs again on a new stack. The
    mutants of a batch whose clean run failed twice are skipped (returned)."""
    skipped: list[str] = []
    for start in range(0, len(names), batch):
        part = names[start : start + batch]
        for attempt in (1, 2):
            if stack.stale() or attempt == 2:
                stack.start()
            day = stack.day
            _say(f"mutants {start + 1}-{start + len(part)} of {len(names)}")
            code = _mutmut("run", "--max-children", "1", *part, env=env)
            if code == 0 and datetime.now(UTC).date().isoformat() == day:
                break
            _say("the UTC day changed or the clean run failed: again, on a new stack")
        else:
            _say("the clean run failed twice: batch skipped (`--debug` shows pytest's output)")
            skipped += part
    return skipped


# ============================================================== report
def _wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = hits / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def _module(mutant: str) -> str:
    return _function_of(mutant).rpartition(".")[0]


def _rows(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def report(front_name: str, work: Plan | None = None, duration: float | None = None) -> None:
    """``report.md`` and ``results.json`` in ``.mutation/<front>/``; the summary on screen."""
    front = FRONTS[front_name]
    work = work or Plan.load()
    sources, status = mutants(), statuses()
    tested = {n: status[n] for n in (work.names or status) if n in status}
    tested |= {n: "flaky" for n in work.flaky if n in tested}
    tested |= {n: "unconfirmed" for n in work.unconfirmed if n in tested}
    counts = Counter(tested.values())
    killed = sum(counts[s] for s in KILLED)
    survived = counts["survived"] + counts["suspicious"]
    lines = [
        f"# Mutation tests: {front_name}",
        "",
        f"{front.help}. Commit {os.environ.get('TA_MUTATION_COMMIT', '?')},"
        f" {datetime.now(UTC):%Y-%m-%d %H:%M} UTC"
        + (f", {duration / 60:.0f} min" if duration else "")
        + ".",
        "",
    ]
    effects = work.effects
    if front.smoke:
        left_out = counts["flaky"], counts["unconfirmed"]
        lines += _smoke_summary(effects, killed, survived, work.seed, left_out)
    else:
        low, high = _wilson(killed, killed + survived)
        sample = (
            f"Random sample of {len(work.names)} of the {len(sources)} mutants (seed {work.seed}). "
            if work.names
            else ""
        )
        lines += [
            f"{sample}**Mutation score: {_pct(killed, killed + survived)}**"
            + (f" (95% CI {low:.1%}-{high:.1%})" if work.names else "")
            + f" of the {killed + survived} mutants the tests reach ({killed} killed,"
            f" {survived} survived); {_pct(killed, killed + survived + counts['no tests'])}"
            f" counting the {counts['no tests']} that no test reaches.",
            "",
        ]
    lines += ["## Status", "", *_rows(["status", "mutants"], sorted(counts.items())), ""]
    by_module: dict[str, Counter[str]] = defaultdict(Counter)
    for name, value in tested.items():
        by_module[_module(name)][value] += 1
    rows = []
    for module, c in sorted(by_module.items()):
        k, s = sum(c[x] for x in KILLED), c["survived"] + c["suspicious"]
        rows.append((module, sum(c.values()), k, s, c["no tests"], _pct(k, k + s)))
    header = ["module", "mutants", "killed", "survived", "no tests", "score"]
    lines += ["## By module", "", *_rows(header, rows), ""]
    survivors = sorted(n for n, v in tested.items() if v in {"survived", "suspicious"})
    # a smoke front mutates one module: its survivors are shown per function
    group = _function_of if front.smoke else _module
    shown = _pick(survivors, group, per_group=5, limit=120)
    diffs = json.loads(_python(DIFFS, stdin=json.dumps([[n, sources[n]] for n in shown])))
    lines += [
        f"## Survivors ({len(survivors)}; up to 5 per {'function' if front.smoke else 'module'}"
        " shown, all in results.json)",
        "",
    ]
    for name in shown:
        extra = effects.get(name, {}).get("changed")
        lines += [f"### `{name}`" + (f" (changes {', '.join(extra)})" if extra else ""), ""]
        lines += ["```diff", diffs[name], "```", ""]
    out = Path(os.environ.get(OUT_ENV, ROOT / ".mutation")) / front_name
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    results = {
        "front": front_name,
        "commit": os.environ.get("TA_MUTATION_COMMIT"),
        "seed": work.seed,
        "tested": sorted(tested),
        "mutants": {n: {"status": status.get(n), "source": sources[n]} for n in sorted(sources)},
        "effects": effects,
    }
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("\n".join(lines[: lines.index("## By module")]))
    _say(f"report: .mutation/{front_name}/report.md")


def _smoke_summary(
    effects: dict[str, dict[str, Any]],
    killed: int,
    survived: int,
    seed: int,
    left_out: tuple[int, int],
) -> list[str]:
    kinds = Counter(e["kind"] for e in effects.values())
    crash, candidates = kinds["crash"], kinds["candidate"]
    tested = killed + survived
    low, high = _wilson(killed, tested)
    p = killed / tested if tested else 0.0
    overall = (crash + p * candidates) / (crash + candidates) if crash + candidates else 0.0
    return [
        f"{len(effects)} mutants of the generator. A static pass (no stack) splits them:"
        f" {crash} break `build()` (every test fails at import: killed), {kinds['no effect']}"
        " give the same dashboards and are not called by the tests directly (no smoke test can"
        f" see them: left out), {candidates} are for the smoke tests to decide.",
        "",
        f"Random sample of {tested} of those {candidates} (seed {seed}),"
        f" run against the smoke tests: {killed} killed (each in two sessions), {survived}"
        f" survived, **{_pct(killed, tested)}** (95% CI {low:.1%}-{high:.1%}). Left out:"
        f" {left_out[0]} flaky (killed once, survived the second time: a test failed for a"
        f" reason of its own) and {left_out[1]} unconfirmed (killed once, not run again: the"
        " clean run of their batch failed twice).",
        "",
        f"**Estimated mutation score: {overall:.1%}** of the {crash + candidates} mutants that"
        " change a dashboard or break the generator.",
        "",
    ]


def _pick(
    names: list[str], group: Callable[[str], str], *, per_group: int, limit: int
) -> list[str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in names:
        grouped[group(name)].append(name)
    picked = [n for members in grouped.values() for n in members[:per_group]]
    return picked[:limit]


# ============================================================== CLI
def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["sync"]:  # inside the image, before the work copy exists (stdlib only)
        sync(Path(argv[1]), Path(argv[2]), Path(argv[3]))
        return 0
    if argv[:1] == ["mutmut"]:  # everything after the front is mutmut's
        own = [a for a in argv[3:] if a in {"--docker", "--local", "--here"}]
        rest = [a for a in argv[3:] if a not in own]
        options = _parser().parse_args([*argv[:3], *own])
        options.mutmut_args = rest
    else:
        options = _parser().parse_args(argv)
    if not options.here:
        return host(options, argv)
    if options.command == "run":
        return execute(options.front, options)
    if options.command == "report":
        report(options.front)
        return 0
    return _mutmut(*options.mutmut_args, env={})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mutation tests (mutmut) of the test suites; see tests/mutation/README.md.",
        epilog="fronts: " + "; ".join(f"{name}: {f.help}" for name, f in FRONTS.items()),
    )
    parser.add_argument("command", choices=["run", "report", "mutmut"])
    parser.add_argument("front", choices=sorted(FRONTS))
    parser.add_argument("--sample", type=int, help="test a random sample of N mutants")
    parser.add_argument("--seed", type=int, default=1, help="of the sample (default 1)")
    parser.add_argument("--max-children", type=int, help="parallel mutants (CPUs - 2, up to 8)")
    parser.add_argument("--batch", type=int, default=25, help="smoke: mutants per stack check")
    parser.add_argument("--max-age", type=float, default=60, help="smoke: minutes per seed")
    parser.add_argument("--debug", action="store_true", help="mutmut's own output (pytest's too)")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--docker", action="store_true", help="in the runner image, also on Linux")
    where.add_argument("--local", action="store_true", help="here (Linux only)")
    parser.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
