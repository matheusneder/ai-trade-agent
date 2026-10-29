"""pytest plugin of the mutation tests of the dashboard smoke tests (tests/mutation/README.md).

mutmut runs each mutant against the tests that called the mutated function while it collected
its statistics. The smoke suites call the generators once per session (fixtures) and at import
(the panels the tests expect come from ``build()``), which would tie every function of a
generator to the first test of the session only. The smoke fronts load this plugin
(``-p tests.support.mutation``), and it:

* in the statistics run, calls the generator in every test of its suite (``build()`` and, in the
  tests with the stack, the provisioning of the dashboards), so every function of the generator
  counts for every test;
* in a mutant's run, builds the dashboards with and without the mutant and keeps only the tests
  of the dashboards it changed (the ``dashboards`` marker, or the test's ``uid``/``name``
  parameter). A mutant that changes no dashboard runs no test (mutmut's "no tests"). The mutants
  of a function the tests or their support code call directly (``apply``, ``check``,
  ``render``...) keep all their tests: ``build()`` alone does not show what they change;
* counts a collection error as a failure: a mutant that breaks ``build()`` breaks the import of
  the test modules, which pytest reports as interrupted (exit code 2), not as failed;
* writes what each mutant changed to ``TA_MUTATION_EFFECTS`` (a JSON per mutant), for the report.
"""

import ast
import functools
import importlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MUTANT_ENV = "MUTANT_UNDER_TEST"  # set by mutmut: a mutant's name, or one of its own modes
EFFECTS_ENV = "TA_MUTATION_EFFECTS"
EVERY = "*"


def _provision_grafana(request: pytest.FixtureRequest) -> None:
    from tests.support.grafana_stack import provision

    provision(request.getfixturevalue("stack"))


def _provision_signoz(request: pytest.FixtureRequest) -> None:
    from tests.support.signoz_smoke.signoz import provision

    provision(request.getfixturevalue("signoz_api"))


@dataclass(frozen=True, slots=True)
class Suite:
    folder: str
    """The suite's folder under ``tests/``."""
    generator: str
    """The module whose dashboards it checks."""
    key: str
    """The field of a dashboard that names it, also the test parameter that holds one."""
    provision: Callable[[pytest.FixtureRequest], None]

    def module(self) -> ModuleType:
        return importlib.import_module(self.generator)


SUITES = (
    Suite("grafana-dashboard-smoke-tests", "scripts.grafana_dashboards", "uid", _provision_grafana),
    Suite("signoz-dashboard-smoke-tests", "scripts.signoz_dashboards", "name", _provision_signoz),
)


def _suite(path: Path) -> Suite | None:
    return next((suite for suite in SUITES if suite.folder in path.parts), None)


def _mutant() -> str:
    return os.environ.get(MUTANT_ENV, "")


def _boards(suite: Suite, mutant: str) -> dict[str, str]:
    """The suite's dashboards as built with ``mutant`` active (``""``: the original code)."""
    previous = _mutant()
    os.environ[MUTANT_ENV] = mutant
    try:
        return {
            str(board[suite.key]): json.dumps(board, sort_keys=True, default=str)
            for board in suite.module().build().values()
        }
    finally:
        os.environ[MUTANT_ENV] = previous


def _names(tree: ast.AST, generator: str) -> Iterator[str]:
    """The names of ``generator`` a module uses: imported from it, or read off its module."""
    package, _, short = generator.rpartition(".")
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == generator:
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == package:
            aliases |= {alias.asname or alias.name for alias in node.names if alias.name == short}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):  # noqa: SIM102
            if node.value.id in aliases:
                yield node.attr


def _original(name: str) -> str | None:
    """A top-level name of the generator as written, from its name in mutmut's tree
    (``x_check__mutmut_orig`` → ``check``); ``None`` for a mutant's copy."""
    if "__mutmut_" not in name:
        return name
    if name.endswith("__mutmut_orig") and name.startswith("x_"):
        return name.removeprefix("x_").removesuffix("__mutmut_orig")
    return None


def _references(suite: Suite) -> dict[str, set[str]]:
    """Each top-level function or class of the generator → the names its code reads."""
    source = Path(str(suite.module().__file__)).read_text(encoding="utf-8")
    graph: dict[str, set[str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = _original(node.name)
            if name is not None:
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                graph.setdefault(name, set()).update(names)
    return graph


@functools.cache
def called_directly(suite: Suite) -> frozenset[str]:
    """The generator's functions and classes the tests reach other than through ``build()``:
    the ones the suite or its support code use by name, and whatever those call in turn."""
    tests = Path("tests")
    files = [*(tests / suite.folder).glob("*.py"), *(tests / "support").rglob("*.py")]
    found: set[str] = set()
    for path in files:
        if path.resolve() != Path(__file__).resolve():
            found.update(_names(ast.parse(path.read_text(encoding="utf-8")), suite.generator))
    graph = _references(suite)
    reached, pending = set(), list(found - {"build"})
    while pending:
        name = pending.pop()
        if name not in reached:
            reached.add(name)
            pending.extend(graph.get(name, set()) & graph.keys())
    return frozenset(reached)


def _function(mutant: str) -> tuple[str, str]:
    """``scripts.x.x_panel__mutmut_3`` → (``scripts.x``, ``panel``); a method gives its class."""
    mangled = mutant.partition("__mutmut_")[0]
    module, _, name = mangled.rpartition(".")
    if name.startswith("x_"):
        return module, name[2:]
    return module, name.split("ǁ")[1]  # xǁClassǁmethod


def _reads(item: pytest.Item, suite: Suite) -> set[str] | None:
    """The dashboards a test reads; ``None`` when it does not say (it always runs)."""
    marker = item.get_closest_marker("dashboards")
    if marker is not None:
        return {str(arg) for arg in marker.args}
    params: dict[str, Any] = getattr(getattr(item, "callspec", None), "params", {})
    if suite.key in params:
        return {str(params[suite.key])}
    return None


def _record(mutant: str, effect: dict[str, Any]) -> None:
    folder = os.environ.get(EFFECTS_ENV)
    if folder:
        path = Path(folder) / f"{mutant}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mutant": mutant, **effect}), encoding="utf-8")


def _changed(suite: Suite, mutant: str) -> set[str] | None:
    """The dashboards ``mutant`` changes; ``None`` when they do not tell (all tests run)."""
    module, function = _function(mutant)
    if module != suite.generator:
        return None
    try:
        mutated = _boards(suite, mutant)
    except Exception as error:  # the tests fail the same way: they tell how
        _record(mutant, {"changed": None, "error": f"{type(error).__name__}: {error}"})
        return None
    clean = _boards(suite, "")
    changed = (set(clean) ^ set(mutated)) | {k for k in clean if mutated.get(k) != clean[k]}
    narrowed = function not in called_directly(suite)
    _record(mutant, {"changed": sorted(changed), "narrowed": narrowed, "error": None})
    return changed if narrowed else None


def _needed(reads: set[str] | None, changed: set[str] | None) -> bool:
    """Whether a test reading ``reads`` can tell a mutant that changed ``changed``."""
    if reads is None or changed is None:
        return True
    return bool(reads & changed) or (EVERY in reads and bool(changed))


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    mutant = _mutant()
    if "__mutmut_" not in mutant:  # statistics, the clean run, the forced failure...
        return
    keep: list[pytest.Item] = []
    drop: list[pytest.Item] = []
    changed_by_suite: dict[Suite, set[str] | None] = {}
    for item in items:
        suite = _suite(item.path)
        if suite is not None and suite not in changed_by_suite:
            changed_by_suite[suite] = _changed(suite, mutant)
        changed = changed_by_suite.get(suite) if suite is not None else None
        reads = _reads(item, suite) if suite is not None else None
        (keep if _needed(reads, changed) else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


@pytest.fixture(autouse=True)
def _every_test_calls_the_generator(request: pytest.FixtureRequest) -> None:
    """Statistics run only: the generator's functions count for every test of its suite."""
    suite = _suite(request.node.path)
    if _mutant() != "stats" or suite is None:
        return
    suite.module().build()
    if request.node.get_closest_marker("e2e"):
        suite.provision(request)


class _Collection:
    failed = False


def pytest_sessionstart() -> None:
    _Collection.failed = False


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _Collection.failed = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    if _Collection.failed and session.exitstatus == pytest.ExitCode.INTERRUPTED:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
