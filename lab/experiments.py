"""The experiment matrix of the pair-local families (D-044), run through ``lab.walk_forward``.

Each experiment is a profile of ``config/profiles.yaml`` with one set of overrides (a
deep merge), written to its own profiles file. Each run gets its own ``user_data`` (the
strategy, its parameter files and results) over the shared candles, so up to ``--jobs`` run
at once (D-044; per-run networks exhausted Docker's address pools, hence ``network_mode:
bridge`` in the compose file). The reports land in ``var/lab`` with the experiment's name.

Groups (doc/lab-results.md, section 2.10):

* ``A``: the three current profiles, unchanged (15 quarterly windows, the documented method);
* ``B``: ``A`` without the benchmark's regime filter (``use_regime_filter: false``);
* ``C``: ``B`` with the local score (``local_score: true``) at four ``min_score`` thresholds;
* ``A_op``: ``A`` sized on the configured capital, in one continuous run (the reference of
  ``D``);
* ``D``: each pair-local family on 1h and 4h, continuous, sized on the configured capital,
  with the scope's whole risk budget (the operation);
* ``E``: the 1h families without the scope's pauses (``no_pauses``: daily loss, losing streak
  and the drawdown review off; the re-entry wait and the open-risk cap stay, they are entry
  rules), then with one component changed at a time. A pause can stop a continuous run for
  good (the drawdown review never expires), so the components are compared on the strategy's
  own edge.

Usage::

    uv run python -m lab.experiments --list
    uv run python -m lab.experiments --only A B --jobs 8
"""

import argparse
import copy
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from trade_agent.strategy.profiles import StrategyConfig, parse_duration

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "config" / "profiles.yaml"
STRATEGY_FILE = ROOT / "lab" / "freqtrade" / "user_data" / "strategies" / "trade_agent_strategy.py"
OUTPUT = ROOT / "var" / "lab"
RUNS = OUTPUT / "runs"
DATA = ROOT / "lab" / "freqtrade" / "user_data" / "data"
LEGACY = ("swing_trend", "momentum_alpha", "alt_atr")
LOCAL = ("pair_pullback_1h", "pair_breakout_1h", "pair_range_1h")
THRESHOLDS = (0.52, 0.57, 0.62, 0.67)
"""``C``: without the relative strength the score tops out at 0.80; these thresholds keep the
next four reachable sums (0.55, 0.60, 0.65 and 0.70)."""
CONTINUOUS = ("--window-months", "45")
NO_PAUSES: dict[str, Any] = {
    "risk": {"daily_loss_pct": None, "consecutive_losses": None, "max_drawdown_pct": None}
}
"""2023-01 to 2026-09 in one run: positions cross the quarters, as in the operation."""


@dataclass(frozen=True)
class Experiment:
    name: str
    group: str
    profile: str
    overrides: dict[str, Any] = field(default_factory=dict)
    args: tuple[str, ...] = ()
    alts: bool = False

    @property
    def label(self) -> str:
        return f"{self.name}-alts" if self.alts else self.name


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep merge: dictionaries merge key by key, anything else replaces."""
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def on_4h(profile: dict[str, Any], code: str) -> dict[str, Any]:
    """The 1h profile's rules on 4h candles: the same rules in candles, so the maximum holding
    keeps its number of candles (×4 in hours)."""
    holding = parse_duration(profile["protection"]["max_holding"])
    assert isinstance(holding, timedelta)  # noqa: S101 - validated by the schema
    hours = int(holding.total_seconds() // 3600) * 4
    return {"code": code, "timeframe": "4h", "protection": {"max_holding": f"{hours}h"}}


def matrix(profiles: dict[str, Any]) -> list[Experiment]:
    """Every experiment of the matrix, each on the tier lists and on ``--alts``."""
    experiments: list[Experiment] = []
    for name in LEGACY:
        no_filter = {"signals": {"use_regime_filter": False}}
        experiments += [
            Experiment(f"A-{name}", "A", name),
            Experiment(f"B-{name}", "B", name, no_filter),
            Experiment(
                f"A_op-{name}", "A_op", name, args=(*CONTINUOUS, "--capital-model", "operational")
            ),
        ]
        for threshold in THRESHOLDS:
            local = merge(no_filter, {"signals": {"local_score": True}})
            local["entry"] = {"min_score": threshold}
            experiments.append(Experiment(f"C{threshold:g}-{name}", "C", name, local))
    continuous = (*CONTINUOUS, "--capital-model", "operational")
    variants: dict[str, dict[str, dict[str, Any]]] = {
        "pair_pullback_1h": {
            "no_pauses": {},
            "no_adx": {"signals": {"adx_min": 0}},
            "no_extension": {"signals": {"max_extension_atr": 0}},
            "no_volume": {"signals": {"vol_rel_min": 0}},
            "obv": {"signals": {"vol_rel_min": 0, "obv_confirm": True}},
            "dip45": {"signals": {"rsi_dip": 45}},
            "trailing": {
                "protection": {
                    "take_profit": {
                        "mode": "trailing",
                        "activation_r": 2.5,
                        "trailing_atr_mult": 1.0,
                    }
                }
            },
        },
        "pair_breakout_1h": {
            "no_pauses": {},
            "no_extension": {"signals": {"max_extension_atr": 0}},
            "no_range": {"signals": {"max_range_atr": 0}},
            "adx": {"signals": {"adx_filter": True}},
            "retest": {"signals": {"retest": True}},
            "trailing15": {"protection": {"take_profit": {"trailing_atr_mult": 1.5}}},
        },
        "pair_range_1h": {"no_pauses": {}},
    }
    for name in LOCAL:
        base = name.removesuffix("_1h")
        code = profiles[name]["code"].replace("1h", "4h")
        experiments += [
            Experiment(f"D-{base}-1h", "D", name, args=continuous),
            Experiment(f"D-{base}-4h", "D", name, on_4h(profiles[name], code), args=continuous),
        ]
        experiments += [
            Experiment(f"E-{base}-{variant}", "E", name, merge(NO_PAUSES, o), args=continuous)
            for variant, o in variants[name].items()
        ]
    return [
        Experiment(e.name, e.group, e.profile, e.overrides, e.args, alts)
        for e in experiments
        for alts in (False, True)
    ]


def profiles_file(config: dict[str, Any], experiment: Experiment, directory: Path) -> Path:
    """A profiles file with the experiment's profile alone (validated)."""
    profile = merge(config["profiles"][experiment.profile], experiment.overrides)
    data = {"account": config["account"], "profiles": {experiment.profile: profile}}
    StrategyConfig.model_validate(data)
    path = directory / "profiles.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def run(experiment: Experiment, config: dict[str, Any], extra: Sequence[str]) -> int:
    directory = RUNS / experiment.label
    if directory.exists():
        shutil.rmtree(directory)
    strategies = directory / "user_data" / "strategies"
    strategies.mkdir(parents=True)
    shutil.copy2(STRATEGY_FILE, strategies)
    profiles = profiles_file(config, experiment, directory)
    command = [
        sys.executable, "-m", "lab.walk_forward", "--profile", experiment.profile,
        "--profiles-file", str(profiles), "--label", experiment.label,
        *experiment.args, *(["--alts"] if experiment.alts else []), *extra,
    ]  # fmt: skip
    env = {
        **os.environ,
        "TA_LAB_USER_DATA": str((directory / "user_data").resolve()),
        "TA_LAB_DATA": str(DATA.resolve()),
    }
    with (directory / "run.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(  # noqa: S603 - command built locally
            command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
        )
    print(f"{experiment.label}: {'ok' if result.returncode == 0 else 'FALHOU'}", flush=True)
    return result.returncode


def done(experiment: Experiment) -> bool:
    """The experiment already has its report in ``var/lab``."""
    return any(OUTPUT.glob(f"walk-forward-*-{experiment.label}-[0-9]*.md"))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", help="grupos ou nomes de experimento")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--resume", action="store_true", help="pula os experimentos que já têm relatório"
    )
    parser.add_argument("--profiles-file", type=Path, default=PROFILES)
    parser.add_argument("extra", nargs="*", help="argumentos extras do walk_forward (após --)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = yaml.safe_load(args.profiles_file.read_text(encoding="utf-8"))
    selected = [
        e
        for e in matrix(config["profiles"])
        if not args.only or {e.group, e.name, e.label} & set(args.only)
    ]
    if args.resume:
        selected = [e for e in selected if not done(e)]
    if args.list:
        for e in selected:
            print(e.group, e.label, " ".join(e.args))
        return 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        codes = list(pool.map(lambda e: run(e, config, args.extra), selected))
    return max(codes, default=0)


if __name__ == "__main__":
    sys.exit(main())
