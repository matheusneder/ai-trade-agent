from pathlib import Path
from typing import Any

import pytest
import yaml
from lab import experiments as ex

from trade_agent.strategy.profiles import load_strategy_config

CONFIG: dict[str, Any] = yaml.safe_load(ex.PROFILES.read_text(encoding="utf-8"))


def test_merge_is_deep_and_leaves_the_base_alone() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    merged = ex.merge(base, {"a": {"b": 9}, "d": [2], "e": 3})
    assert merged == {"a": {"b": 9, "c": 2}, "d": [2], "e": 3}
    assert base == {"a": {"b": 1, "c": 2}, "d": [1]}


def test_the_4h_variant_keeps_the_rules_in_candles() -> None:
    assert ex.on_4h(CONFIG["profiles"]["pair_pullback_1h"], "ppb4h") == {
        "code": "ppb4h",
        "timeframe": "4h",
        "protection": {"max_holding": "192h"},
    }


def test_the_matrix_covers_every_group_on_both_universes(tmp_path: Path) -> None:
    experiments = ex.matrix(CONFIG["profiles"])
    labels = [e.label for e in experiments]
    assert len(labels) == len(set(labels))
    groups = {g: sum(e.group == g for e in experiments) for g in ("A", "B", "C", "A_op", "D", "E")}
    assert groups == {"A": 6, "B": 6, "C": 24, "A_op": 6, "D": 12, "E": 22}
    assert all(e.alts == e.label.endswith("-alts") for e in experiments)
    for experiment in experiments:  # every profile file is a valid configuration
        directory = tmp_path / experiment.label
        directory.mkdir()
        config = load_strategy_config(ex.profiles_file(CONFIG, experiment, directory))
        assert list(config.profiles) == [experiment.profile]
    b = next(e for e in experiments if e.name == "B-swing_trend")
    profile = ex.merge(CONFIG["profiles"]["swing_trend"], b.overrides)
    assert profile["signals"]["use_regime_filter"] is False
    assert profile["signals"]["min_stop_atr"] == 1.5  # the rest of the profile unchanged
    c = next(e for e in experiments if e.name == "C0.57-alt_atr")
    assert c.overrides == {
        "signals": {"use_regime_filter": False, "local_score": True},
        "entry": {"min_score": 0.57},
    }
    d = next(e for e in experiments if e.name == "D-pair_breakout-4h")
    assert d.overrides["timeframe"] == "4h" and "--capital-model" in d.args


def test_run_gives_each_experiment_its_own_user_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ex, "RUNS", tmp_path)
    seen: dict[str, Any] = {}

    class Result:
        returncode = 0

    def fake_run(command: list[str], **kwargs: Any) -> Result:
        seen["command"], seen["env"] = command, kwargs["env"]
        return Result()

    monkeypatch.setattr("lab.experiments.subprocess.run", fake_run)
    label = "D-pair_pullback-1h-alts"
    experiment = next(e for e in ex.matrix(CONFIG["profiles"]) if e.label == label)
    (tmp_path / experiment.label).mkdir()  # a previous run is replaced
    assert ex.run(experiment, CONFIG, ["--end", "2023-04-01"]) == 0
    directory = tmp_path / experiment.label
    assert (directory / "user_data" / "strategies" / "trade_agent_strategy.py").exists()
    assert seen["env"]["TA_LAB_USER_DATA"] == str((directory / "user_data").resolve())
    assert seen["command"][-3:] == ["--alts", "--end", "2023-04-01"]
    assert "--label" in seen["command"] and experiment.label in seen["command"]


def test_main_lists_and_runs_the_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ex.main(["--list", "--only", "D"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 12 and all(line.startswith("D ") for line in listed)
    ran: list[str] = []

    def fake_run(experiment: ex.Experiment, config: Any, extra: Any) -> int:
        ran.append(experiment.label)
        return 3

    monkeypatch.setattr(ex, "run", fake_run)
    assert ex.main(["--only", "B-alt_atr-alts", "--jobs", "1"]) == 3
    assert ran == ["B-alt_atr-alts"]
