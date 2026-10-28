"""The lab strategy runs the pair-local families (D-044) as the agent would.

Backtests ``pair_pullback_1h`` (fixed target) and ``pair_breakout_1h`` (trailing from 3R, exit
below the broken level) of ``config/profiles.yaml`` in the pinned image, on a few tier pairs
with 5m detail, and checks every trade against the family's own frames computed here with
``trade_agent.signals``:

* the strategy used every value of the profile (``trade_agent_effective.json``);
* entries only at the open of the candle after the signal, on a candle with the setup;
* a stop never fills above ``entry × (1 − stop)``, the signal candle's stop, give or take the
  tick Freqtrade rounds it up to; a fixed target fills exactly at
  ``entry × (1 + 1.5 × (stop + cost) + cost)``, as a native limit order;
* a technical exit follows a closed candle with the exit rule (the breakout's: a close below
  the level its entry broke); a maximum-holding exit comes after the maximum holding.

Needs Docker, the pinned image and the lab's 1h and 5m candles; skipped without them.
"""

import gzip
import json
import shutil
import subprocess
import zipfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from lab import walk_forward as wf

from tests.integration.lab.test_lab_causality import (
    LAB,
    ROOT,
    _data_dir,
    _docker,
    _image,
    _require_image,
)
from trade_agent.risk.conditions import load_stop_conditions
from trade_agent.signals import compute_features, compute_local_features, local_frame
from trade_agent.strategy.profiles import ProfileConfig, load_strategy_config

PAIRS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT", "AVAX/USDT", "ADA/USDT", "DOT/USDT")
START, END = "20230101", "20240101"


def _require_data() -> None:
    _require_image()
    names = [f"{p.replace('/', '_')}-{tf}.json.gz" for p in PAIRS for tf in ("1h", "5m")]
    missing = [n for n in names if not (_data_dir() / n).exists()]
    if missing:
        pytest.skip(f"lab candles missing in {_data_dir()}: {missing[:3]}")


def _profile(name: str) -> ProfileConfig:
    return load_strategy_config(ROOT / "config" / "profiles.yaml").profiles[name]


def _backtest(tmp: Path, profile_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    user_data = tmp / profile_name / "user_data"
    data = user_data / "data" / "binance"
    data.mkdir(parents=True)
    (user_data / "strategies").mkdir()
    (user_data / "backtest_results").mkdir()
    strategy_file = LAB / "user_data" / "strategies" / "trade_agent_strategy.py"
    shutil.copy2(strategy_file, user_data / "strategies")
    for pair in PAIRS:
        for timeframe in ("1h", "5m"):
            name = f"{pair.replace('/', '_')}-{timeframe}.json.gz"
            shutil.copy2(_data_dir() / name, data / name)
    profile = _profile(profile_name)
    pre_trade = load_stop_conditions(ROOT / "config" / "stop_conditions.yaml").pre_trade
    base = json.loads((LAB / "user_data" / "config.base.json").read_text(encoding="utf-8"))
    lab_config = wf.lab_config(base, profile, Decimal(100), PAIRS)
    (user_data / "config.json").write_text(json.dumps(lab_config), encoding="utf-8")
    params = wf.strategy_params(profile, pre_trade)
    manifest = wf.profile_manifest(profile, Decimal(100), "operational", pre_trade)
    strategies = user_data / "strategies"
    (strategies / wf.PARAMS_FILE).write_text(json.dumps(params), encoding="utf-8")
    (strategies / wf.PROFILE_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(  # noqa: S603 - fixed arguments, the paths are the test's own
        [
            _docker(), "run", "--rm", "--network", "bridge",
            "-v", f"{user_data.as_posix()}:/freqtrade/user_data",
            "-v", f"{(ROOT / 'src').as_posix()}:/opt/trade_agent_src:ro",
            "-e", "PYTHONPATH=/opt/trade_agent_src",
            "-w", "/freqtrade", _image(),
            "backtesting", "--config", "user_data/config.json", "--strategy", wf.STRATEGY,
            "--timerange", f"{START}-{END}", "--timeframe-detail", "5m", "--export", "trades",
            "--backtest-directory", "user_data/backtest_results", "--cache", "none",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    effective = json.loads((strategies / wf.EFFECTIVE_FILE).read_text(encoding="utf-8"))
    wf.check_effective(params, manifest, effective)
    events = (strategies / wf.EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    assert not [e for e in events if json.loads(e)["kind"] == "error"]
    archive = wf.latest_result(user_data / "backtest_results")
    with zipfile.ZipFile(archive) as zipped:
        name = next(
            n
            for n in zipped.namelist()
            if n.endswith(".json") and "_config" not in n and "_market" not in n
            and not n.endswith("TradeAgentStrategy.json")
        )  # fmt: skip
        trades: list[dict[str, Any]] = json.loads(zipped.read(name))["strategy"][wf.STRATEGY][
            "trades"
        ]
    return trades, manifest


def _candles(pair: str) -> pd.DataFrame:
    path = _data_dir() / f"{pair.replace('/', '_')}-1h.json.gz"
    rows = json.loads(gzip.decompress(path.read_bytes()))
    candles = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    candles.index = pd.to_datetime(candles["t"], unit="ms", utc=True)
    return candles.drop(columns="t")


def _tick(pair: str, at: pd.Timestamp) -> float:
    """The pair's price tick in the backtest at ``at``: Freqtrade takes it from the history,
    as 10 ** −(the most decimals of the month's prices) (``get_tick_size_over_time``)."""
    month = _candles(pair).loc[at.strftime("%Y-%m"), ["open", "high", "low", "close"]]
    decimals = max(
        len(
            np.format_float_positional(price, precision=14, unique=False, fractional=False)
            .partition(".")[2]
            .rstrip("0")
        )
        for price in month.to_numpy().ravel()
    )
    return 10.0**-decimals


def _frame(pair: str, profile: ProfileConfig) -> pd.DataFrame:
    features = compute_local_features(compute_features(_candles(pair)))
    frame = local_frame(features, profile.local_params())
    return frame.join(features[["close", "ema_short", "rsi"]])


def _check(
    trades: list[dict[str, Any]], manifest: dict[str, Any], profile: ProfileConfig
) -> dict[str, int]:
    cost = manifest["risk"]["cost_pct"] / 100
    hour = pd.Timedelta("1h")
    reasons: dict[str, int] = {}
    for trade in trades:
        opened = pd.Timestamp(trade["open_timestamp"], unit="ms", tz="UTC")
        closed = pd.Timestamp(trade["close_timestamp"], unit="ms", tz="UTC")
        frame = _frame(trade["pair"], profile)
        assert opened == opened.floor("1h")  # entries only at a candle's open
        signal = frame.loc[opened - hour]
        assert signal["setup"] != ""
        stop: float = frame["stop_pct"].astype(float).loc[opened - hour]
        reason = trade["exit_reason"]
        reasons[reason] = reasons.get(reason, 0) + 1
        if reason == "stop_loss":
            # at the agent's OCO stop or below it (a gap), up to the tick Freqtrade rounds the
            # stop price up to; before lab-results.md 2.11 it sat ~0.2% higher
            tick = _tick(trade["pair"], opened)
            assert trade["close_rate"] <= trade["open_rate"] * (1 - stop) + tick * (1 + 1e-6)
        elif reason == "roi":
            target = 1.5 * (stop + cost) + cost
            # at the target, rounded to the pair's tick like the native limit order
            assert trade["close_rate"] == pytest.approx(trade["open_rate"] * (1 + target), rel=5e-4)
        elif reason in ("exit_signal", "technical_exit"):
            last = frame.loc[closed.floor("1h") - hour]  # the candle closed at the exit
            if reason == "exit_signal":
                assert last["exit"]
            else:
                level: float = frame["level"].astype(float).loc[opened - hour]
                assert last["close"] < level
        elif reason == "max_holding":
            assert closed - opened >= timedelta(hours=48)
    return reasons


def test_pullback_entries_targets_and_exits_match_the_family(tmp_path: Path) -> None:
    _require_data()
    trades, manifest = _backtest(tmp_path, "pair_pullback_1h")
    assert trades  # not vacuous
    reasons = _check(trades, manifest, _profile("pair_pullback_1h"))
    assert set(reasons) <= {"roi", "stop_loss", "exit_signal", "max_holding", "force_exit"}


def test_breakout_exits_below_the_level_its_entry_broke(tmp_path: Path) -> None:
    _require_data()
    trades, manifest = _backtest(tmp_path, "pair_breakout_1h")
    reasons = _check(trades, manifest, _profile("pair_breakout_1h"))
    assert reasons.get("technical_exit", 0) > 0
    assert set(reasons) <= {
        "stop_loss", "trailing_stop_loss", "technical_exit", "max_holding", "force_exit",
    }  # fmt: skip
