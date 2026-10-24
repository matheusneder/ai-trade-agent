"""The lab strategy is causal: changing the candles after ``CUT`` cannot change anything before it.

Runs the pinned Freqtrade image's backtest twice on alts with candles since 2022-10-01, i.e. 552
4h candles before the window, fewer than the 999-candle startup (D-040). With the startup trimmed
by date, Freqtrade's callback cursor (``startup_candle_count + row``) runs ahead of time for those
pairs; the strategy must never read it. The second run rewrites the alts' 4h candles from
``CUT`` on, and every entry before ``CUT`` (time, price, size) and every exit before ``CUT``
(time, price, reason) must be identical.

Needs Docker, the pinned image and the lab's candles (``LAB_DATA_DIR``, default
``lab/freqtrade/user_data/data/binance``); it is skipped without them (as on CI).
"""

import gzip
import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from lab import walk_forward as wf

from trade_agent.risk.conditions import load_stop_conditions
from trade_agent.strategy.profiles import load_strategy_config

ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "lab" / "freqtrade"
SHORT_HISTORY = ("INJ/USDT", "CFX/USDT", "RLC/USDT", "AXS/USDT", "OGN/USDT", "API3/USDT")
PAIRS = ("BTC/USDT", "ETH/USDT", *SHORT_HISTORY)
START, END = "20230101", "20230401"
CUT = datetime(2023, 2, 1, tzinfo=UTC)
CUT_MS = int(CUT.timestamp() * 1000)


def _image() -> str:
    match = re.search(r"image:\s*(\S+)", (LAB / "docker-compose.yml").read_text(encoding="utf-8"))
    assert match is not None
    return match.group(1)


def _data_dir() -> Path:
    return Path(os.environ.get("LAB_DATA_DIR", LAB / "user_data" / "data" / "binance"))


def _files(pair: str) -> list[str]:
    base = pair.replace("/", "_")
    return [f"{base}-4h.json.gz", f"{base}-5m.json.gz"]


def _docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not available")
    return docker


def _require_environment() -> None:
    image = subprocess.run(  # noqa: S603 - fixed arguments, the image comes from the repository
        [_docker(), "image", "inspect", _image()], capture_output=True, check=False
    )
    if image.returncode != 0:
        pytest.skip("the pinned Freqtrade image is not pulled")
    missing = [f for p in PAIRS for f in _files(p) if not (_data_dir() / f).exists()]
    if missing:
        pytest.skip(f"lab candles missing in {_data_dir()}: {missing[:3]}")


def _user_data(root: Path, *, rewrite_future: bool) -> Path:
    user_data = root / "user_data"
    data = user_data / "data" / "binance"
    data.mkdir(parents=True)
    (user_data / "strategies").mkdir()
    (user_data / "backtest_results").mkdir()
    strategy_file = LAB / "user_data" / "strategies" / "trade_agent_strategy.py"
    shutil.copy2(strategy_file, user_data / "strategies")
    for pair in PAIRS:
        for name in _files(pair):
            shutil.copy2(_data_dir() / name, data / name)
        if rewrite_future and pair in SHORT_HISTORY:
            path = data / _files(pair)[0]
            candles = json.loads(gzip.decompress(path.read_bytes()))
            # A different future that changes what the callbacks read: flat candles at 3x
            # the last price before the cut. The ATR% falls towards zero (a narrower stop and
            # a larger size) and the close sits far above any entry (break-even). A uniform
            # rescale would not do: the ATR% and the capped stop would stay the same.
            last_close = next(c[4] for c in reversed(candles) if c[0] < CUT_MS)
            for candle in candles:
                if candle[0] >= CUT_MS:
                    candle[1:5] = [last_close * 3] * 4
            path.write_bytes(gzip.compress(json.dumps(candles).encode()))

    strategy = load_strategy_config(ROOT / "config" / "profiles.yaml")
    profile = strategy.profiles["momentum_alpha"]
    # without the stop-noise filter (D-039), so the alts trade early in the window
    signals = {k: v for k, v in profile.signals.items() if k != "min_stop_atr"}
    profile = profile.model_copy(update={"signals": signals})
    pre_trade = load_stop_conditions(ROOT / "config" / "stop_conditions.yaml").pre_trade
    base = json.loads((LAB / "user_data" / "config.base.json").read_text(encoding="utf-8"))
    config = wf.lab_config(base, profile, Decimal(1000), PAIRS)
    (user_data / "config.json").write_text(json.dumps(config), encoding="utf-8")
    params = wf.strategy_params(profile, pre_trade)
    (user_data / "strategies" / wf.PARAMS_FILE).write_text(json.dumps(params), encoding="utf-8")
    return user_data


def _trades(user_data: Path) -> list[dict[str, Any]]:
    subprocess.run(  # noqa: S603 - fixed arguments, the paths are the test's own
        [
            _docker(), "run", "--rm",
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
    archive = wf.latest_result(user_data / "backtest_results")
    with zipfile.ZipFile(archive) as zipped:
        name = next(
            n
            for n in zipped.namelist()
            if n.endswith(".json") and "_config" not in n and "_market" not in n
        )
        trades: list[dict[str, Any]] = json.loads(zipped.read(name))["strategy"][wf.STRATEGY][
            "trades"
        ]
    return trades


Rows = list[tuple[Any, ...]]


def _before_cut(trades: list[dict[str, Any]]) -> tuple[Rows, Rows]:
    def at(value: int) -> datetime:
        return datetime.fromtimestamp(value / 1000, UTC)

    entries = sorted(
        (t["pair"], t["open_timestamp"], t["open_rate"], t["amount"], t["stake_amount"])
        for t in trades
        if at(t["open_timestamp"]) < CUT
    )
    exits = sorted(
        (t["pair"], t["open_timestamp"], t["close_timestamp"], t["close_rate"], t["exit_reason"])
        for t in trades
        if at(t["close_timestamp"]) < CUT
    )
    return entries, exits


def test_changing_future_candles_does_not_change_earlier_sizing_or_exits(tmp_path: Path) -> None:
    _require_environment()
    original = _trades(_user_data(tmp_path / "original", rewrite_future=False))
    rewritten = _trades(_user_data(tmp_path / "rewritten", rewrite_future=True))

    entries, exits = _before_cut(original)
    # not vacuous: short-history alts trade before the cut in this window
    assert any(pair in SHORT_HISTORY for pair, *_ in entries)
    assert any(pair in SHORT_HISTORY for pair, *_ in exits)
    assert _before_cut(rewritten) == (entries, exits)
