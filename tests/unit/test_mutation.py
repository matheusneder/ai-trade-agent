"""The mutation runner (scripts/mutation.py) and the smoke fronts' plugin
(tests/support/mutation.py): what decides what runs and what the report says, without Docker or
mutmut."""

import ast
import json
import tomllib
from pathlib import Path

import pytest
from scripts import mutation as runner

from tests.support import mutation as plugin

SIGNOZ = next(suite for suite in plugin.SUITES if suite.folder == "signoz-dashboard-smoke-tests")
GRAFANA = next(suite for suite in plugin.SUITES if suite.folder == "grafana-dashboard-smoke-tests")


def test_mutant_names_give_the_mutated_function_or_class() -> None:
    assert plugin._function("scripts.signoz_dashboards.x__operation__mutmut_12") == (
        "scripts.signoz_dashboards",
        "_operation",
    )
    assert plugin._function("scripts.signoz_dashboards.xǁSigNozSettingsǁx__mutmut_1") == (
        "scripts.signoz_dashboards",
        "SigNozSettings",
    )
    assert runner._module("trade_agent.risk.guard.xǁRiskGuardǁcheck__mutmut_3") == (
        "trade_agent.risk.guard"
    )


@pytest.mark.parametrize(
    ("reads", "changed", "needed"),
    [
        (None, {"a"}, True),  # the test does not say what it reads: it always runs
        ({"a"}, None, True),  # the mutant's effect is unknown: every test runs
        ({"a"}, {"a", "b"}, True),
        ({"a"}, {"b"}, False),
        ({"*"}, {"b"}, True),
        ({"*"}, set(), False),
        (set(), {"a"}, False),  # reads no dashboard
    ],
)
def test_a_test_runs_only_for_the_dashboards_it_reads(
    reads: set[str] | None, changed: set[str] | None, needed: bool
) -> None:
    assert plugin._needed(reads, changed) is needed


def test_names_used_from_the_generator_are_found_both_ways() -> None:
    tree = ast.parse(
        "from scripts.grafana_dashboards import build, render\n"
        "from scripts import signoz_dashboards as gen\n"
        "gen.check(1)\nother.apply(2)\n"
    )
    assert set(plugin._names(tree, "scripts.grafana_dashboards")) == {"build", "render"}
    assert set(plugin._names(tree, "scripts.signoz_dashboards")) == {"check"}


def test_only_build_mutants_are_narrowed_to_their_dashboards() -> None:
    # the tests call these directly (or through them): build() alone does not show their effect
    direct = plugin.called_directly(SIGNOZ)
    assert {"apply", "check", "existing", "events", "_builder", "_queries", "_data"} <= direct
    assert not {"build", "_operation", "_health", "_llm", "_containers", "dashboard"} & direct
    assert plugin.called_directly(GRAFANA) >= {"render"}
    assert "panel" not in plugin.called_directly(GRAFANA)


def test_original_names_in_mutmut_tree() -> None:
    assert plugin._original("x_check__mutmut_orig") == "check"
    assert plugin._original("x_check__mutmut_7") is None
    assert plugin._original("check") == "check"


def test_wilson_interval() -> None:
    low, high = runner._wilson(76, 125)
    assert low == pytest.approx(0.520, abs=1e-3)
    assert high == pytest.approx(0.689, abs=1e-3)
    assert runner._wilson(0, 0) == (0.0, 0.0)


def test_sync_copies_what_changed_and_drops_what_left(tmp_path: Path) -> None:
    source, work = tmp_path / "repo", tmp_path / "work"
    (source / "a").mkdir(parents=True)
    (source / "a" / "x.py").write_text("x = 1", encoding="utf-8")
    (source / "y.py").write_text("y = 1", encoding="utf-8")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("a/x.py\ny.py\ngone.py", encoding="utf-8")
    runner.sync(source, work, manifest)
    assert (work / "a" / "x.py").read_text(encoding="utf-8") == "x = 1"
    (work / "mutants").mkdir()  # mutmut's tree stays
    (source / "a" / "x.py").write_text("x = 2", encoding="utf-8")
    manifest.write_text("a/x.py", encoding="utf-8")
    runner.sync(source, work, manifest)
    assert (work / "a" / "x.py").read_text(encoding="utf-8") == "x = 2"
    assert not (work / "y.py").exists()
    assert (work / "mutants").is_dir()


def test_each_front_writes_its_mutmut_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    names = "src/trade_agent/a.py\nscripts/b.py\nconfig/c.yaml\nuv.lock"
    Path(".manifest").write_text(names, encoding="utf-8")
    runner._configure(runner.FRONTS["integration"])
    runner._configure(runner.FRONTS["smoke-grafana"])  # replaces the section, never appends
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    assert config["source_paths"] == ["scripts"]
    assert config["only_mutate"] == ["scripts/grafana_dashboards.py"]
    assert config["also_copy"] == ["config", "src"]
    assert config["pytest_add_cli_args"][-2:] == ["-p", "tests.support.mutation"]
    assert "pytest_playwright" not in Path(".mutation-preload.txt").read_text(encoding="utf-8")
    runner._configure(runner.FRONTS["integration"])
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    assert "--ignore=tests/integration/lab/test_lab_causality.py" in config["pytest_add_cli_args"]


def test_a_new_sample_forgets_earlier_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    meta = Path("mutants/scripts/g.py.meta")
    meta.parent.mkdir(parents=True)
    codes = {"scripts.g.x_f__mutmut_1": 1, "scripts.g.x_f__mutmut_2": 0}
    meta.write_text(json.dumps({"exit_code_by_key": codes}), encoding="utf-8")
    runner._forget({"scripts.g.x_f__mutmut_2"})
    assert runner.statuses() == {
        "scripts.g.x_f__mutmut_1": "killed",
        "scripts.g.x_f__mutmut_2": "not checked",
    }
    assert runner.mutants() == dict.fromkeys(codes, "scripts/g.py")
