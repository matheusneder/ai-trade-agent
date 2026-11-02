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
    assert groups == {"A": 6, "B": 6, "C": 24, "A_op": 6, "D": 12, "E": 28}
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
    e = next(e for e in experiments if e.name == "E-pair_pullback-obv")
    assert e.overrides == {
        "risk": {"daily_loss_pct": None, "consecutive_losses": None, "max_drawdown_pct": None},
        "signals": {"vol_rel_min": 0, "obv_confirm": True},
    }


def test_the_review_matrix_runs_the_forward_window_and_the_break_even_variants(
    tmp_path: Path,
) -> None:
    experiments = ex.review_matrix(CONFIG["profiles"])
    labels = [e.label for e in experiments]
    assert len(labels) == len(set(labels)) == 44
    groups = {g: sum(e.group == g for e in experiments) for g in ex.REVIEW_GROUPS}
    assert groups == {
        "R": 6, "Rbe": 6, "Rnobe": 4, "Rfwd": 6, "Rfwd_be": 6, "Rdemo": 4, "Rfinal": 6,
        "Rfinal_fwd": 6,
    }  # fmt: skip
    assert all(e.alts == e.label.endswith("-alts") for e in experiments)
    for experiment in experiments:  # every profile file is a valid configuration
        directory = tmp_path / experiment.label
        directory.mkdir()
        config = load_strategy_config(ex.profiles_file(CONFIG, experiment, directory))
        (profile,) = config.profiles.values()
        if experiment.group in ("Rfinal", "Rfinal_fwd"):
            assert experiment.overrides == {}  # the repository profiles as they are
            continue
        assert profile.protection.break_even_intrabar == (experiment.group in ("Rbe", "Rfwd_be"))
        if experiment.group == "Rnobe":
            assert profile.protection.break_even_after_r is None
        if experiment.profile != "alt_atr" and experiment.group != "Rdemo":
            assert profile.allocation.risk_per_trade_pct == 1.5  # the reviewed value (D-047)
    forward = next(e for e in experiments if e.label == "Rfwd-momentum_alpha-alts")
    assert forward.args[:6] == ex.FORWARD
    assert forward.args[6] == "--pairs"
    pairs = forward.args[7:]
    assert "BTC/USDT" in pairs and "ATOM/USDT" in pairs and "STRK/USDT" in pairs
    assert next(e for e in experiments if e.label == "Rfwd-momentum_alpha").args == ex.FORWARD
    alt = next(e for e in experiments if e.label == "Rfwd-alt_atr-alts")
    assert "BTC/USDT" not in alt.args  # alt_atr has no core tier
    demo = next(e for e in experiments if e.label == "Rdemo-swing_trend")
    swing = ex.merge(CONFIG["profiles"]["swing_trend"], demo.overrides)
    assert swing["allocation"]["risk_per_trade_pct"] == 1.2
    assert swing["protection"]["take_profit"]["activation_pct"] == 9.7
    assert swing["protection"]["break_even_intrabar"] is False
    assert swing["signals"]["min_stop_atr"] == 0.0
    momentum = ex.merge(CONFIG["profiles"]["momentum_alpha"], ex.DEMO_ERA["momentum_alpha"])
    assert momentum["allocation"]["risk_per_trade_pct"] == 1.5
    assert momentum["protection"]["stop"]["max_pct"] == 5.0
    assert ex.tier_pairs(CONFIG["profiles"]["alt_atr"])[0] == "SOL/USDT"


def test_main_selects_the_review_matrix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ex.main(["--list", "--matrix", "review", "--only", "Rdemo"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 4 and all(line.startswith("Rdemo ") for line in listed)


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


def test_resume_skips_the_experiments_with_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ex, "OUTPUT", tmp_path)
    (tmp_path / "walk-forward-x-B-alt_atr-20261028-1.md").write_text("", encoding="utf-8")
    assert ex.main(["--list", "--only", "B-alt_atr", "B-alt_atr-alts", "--resume"]) == 0
    assert capsys.readouterr().out.split() == ["B", "B-alt_atr-alts"]
