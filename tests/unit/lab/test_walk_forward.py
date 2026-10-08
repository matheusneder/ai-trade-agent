import argparse
import gzip
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from lab import walk_forward as wf

from trade_agent.strategy.profiles import load_strategy_config

PROFILES = wf.ROOT / "tests" / "fixtures" / "profiles.yaml"
CONFIG = load_strategy_config(PROFILES)
BASE = json.loads((wf.USER_DATA / "config.base.json").read_text(encoding="utf-8"))


def test_windows_and_timerange() -> None:
    assert wf.add_months(date(2025, 11, 1), 3) == date(2026, 2, 1)
    assert wf.add_months(date(2025, 3, 1), -3) == date(2024, 12, 1)
    periods = wf.windows(date(2025, 1, 1), date(2025, 7, 15), 3)
    assert periods == [
        (date(2025, 1, 1), date(2025, 4, 1)),
        (date(2025, 4, 1), date(2025, 7, 1)),
        (date(2025, 7, 1), date(2025, 7, 15)),
    ]
    assert wf.timerange(*periods[0]) == "20250101-20250401"
    with pytest.raises(ValueError, match="janela"):
        wf.windows(date(2025, 1, 1), date(2025, 1, 1), 3)
    with pytest.raises(ValueError, match="janela"):
        wf.windows(date(2025, 1, 1), date(2025, 2, 1), 0)
    assert wf.train_range(date(2025, 4, 1), 12) == (date(2024, 4, 1), date(2025, 4, 1))
    with pytest.raises(ValueError, match="treino"):
        wf.train_range(date(2025, 4, 1), 0)


def test_profile_pairs_follow_tiers() -> None:
    conservative = wf.profile_pairs(CONFIG.profiles["conservador"])
    assert conservative[:2] == ["BTC/USDT", "ETH/USDT"]
    assert "ATOM/USDT" not in conservative
    aggressive = wf.profile_pairs(CONFIG.profiles["agressivo"])
    assert "GALA/USDT" in aggressive


def test_lab_config_from_profile() -> None:
    config = wf.lab_config(BASE, CONFIG.profiles["moderado"], Decimal("300"))
    assert "$comment" not in config
    assert config["timeframe"] == "1h"
    assert config["max_open_trades"] == 5
    assert config["tradable_balance_ratio"] == pytest.approx(0.8)
    assert config["dry_run_wallet"] == 300.0
    assert "ATOM/USDT" in config["exchange"]["pair_whitelist"]
    assert BASE["exchange"]["pair_whitelist"] == []  # the base is not changed


def test_strategy_params_fixed_and_trailing_stop() -> None:
    fixed = wf.strategy_params(CONFIG.profiles["conservador"])["params"]
    assert fixed["buy"]["min_score"] == 0.6
    assert fixed["buy"]["risk_per_trade"] == pytest.approx(0.005)
    assert fixed["sell"]["stop_max_pct"] == pytest.approx(0.04)
    assert fixed["sell"]["tp_activation"] == pytest.approx(0.03)
    assert fixed["sell"]["tp_trailing"] == pytest.approx(0.01)
    assert fixed["sell"]["atr_stop_mult"] == 2.0
    trailing = wf.strategy_params(CONFIG.profiles["agressivo"])["params"]["sell"]
    assert trailing["stop_max_pct"] == pytest.approx(0.08)
    assert trailing["atr_stop_mult"] == 100.0


def _exported(**sell: float) -> dict[str, object]:
    return {
        "strategy_name": wf.STRATEGY,
        "params": {
            # Freqtrade also exports the non-optimized ones; the risk ones must be ignored
            "buy": {"min_score": 0.45, "adx_min": 25, "risk_per_trade": 0.02},
            "sell": {"tp_activation": 0.07, **sell},
            "roi": {"0": 100},
        },
    }


def test_merge_optimized_keeps_profile_risk_parameters() -> None:
    base = wf.strategy_params(CONFIG.profiles["moderado"])
    merged = wf.merge_optimized(base, _exported())
    assert merged["params"]["buy"]["min_score"] == 0.45
    assert merged["params"]["buy"]["risk_per_trade"] == pytest.approx(0.01)  # from the profile
    assert merged["params"]["sell"]["tp_activation"] == 0.07
    assert merged["params"]["sell"]["stop_max_pct"] == pytest.approx(0.07)
    assert base["params"]["buy"]["min_score"] == 0.5  # the base is not changed
    assert wf.chosen_params(_exported()) == {
        "min_score": 0.45,
        "adx_min": 25.0,
        "tp_activation": 0.07,
    }


def _jsongz(path: Path, candles: list[list[float]]) -> Path:
    path.write_bytes(gzip.compress(json.dumps(candles).encode()))
    return path


def test_buy_and_hold_from_local_candles(tmp_path: Path) -> None:
    day = 86_400_000
    jan1 = 1_735_689_600_000  # 2025-01-01T00:00Z
    data = _jsongz(
        tmp_path / "BTC_USDT-1d.json.gz",
        [
            [jan1 - day, 90.0, 0, 0, 95.0, 1],
            [jan1, 100.0, 0, 0, 101.0, 1],
            [jan1 + day, 101.0, 0, 0, 110.0, 1],
            [jan1 + 2 * day, 110.0, 0, 0, 130.0, 1],
        ],
    )
    assert wf.buy_and_hold_pct(data, date(2025, 1, 1), date(2025, 1, 3)) == pytest.approx(10.0)
    assert wf.buy_and_hold_pct(data, date(2026, 1, 1), date(2026, 2, 1)) is None
    assert (
        wf.buy_and_hold_pct(tmp_path / "missing.json.gz", date(2025, 1, 1), date(2025, 2, 1))
        is None
    )


def _stats(**overrides: object) -> dict[str, object]:
    stats: dict[str, object] = {
        "total_trades": 12,
        "profit_total": 0.034,
        "winrate": 0.5,
        "max_drawdown_account": 0.021,
        "market_change": -0.05,
    }
    stats.update(overrides)
    return {"strategy": {wf.STRATEGY: stats}}


def test_parse_result_zip_and_json(tmp_path: Path) -> None:
    archive = tmp_path / "backtest-result-x.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("backtest-result-x_config.json", "{}")
        zipped.writestr("backtest-result-x_market_change.json", "{}")
        zipped.writestr("backtest-result-x.json", json.dumps(_stats()))
    result = wf.parse_result(archive, date(2025, 1, 1), date(2025, 4, 1))
    assert result.trades == 12
    assert result.profit_pct == pytest.approx(3.4)
    assert result.excess_pct == pytest.approx(8.4)
    plain = tmp_path / "r.json"
    plain.write_text(json.dumps(_stats(winrate=None)), encoding="utf-8")
    assert wf.parse_result(plain, date(2025, 1, 1), date(2025, 4, 1)).winrate_pct == 0.0
    (tmp_path / ".last_result.json").write_text(json.dumps({"latest_backtest": archive.name}))
    assert wf.latest_result(tmp_path) == archive


def test_summarize() -> None:
    results = [
        wf.WindowResult(date(2025, 1, 1), date(2025, 4, 1), 10, 5.0, 60.0, 3.0, 2.0, 4.0),
        wf.WindowResult(date(2025, 4, 1), date(2025, 7, 1), 4, -2.0, 25.0, 4.5, -6.0, -3.0),
    ]
    report = wf.summarize("conservador", results, "Parâmetros fixos.")
    assert "Parâmetros fixos." in report
    assert "| 2025-01-01 → 2025-04-01 | 10 | 5.00 | 2.00 | 4.00 | 3.00 | 60.0 | 3.00 |" in report
    assert "positivo: **1/2**" in report
    assert "buy & hold da cesta: **2/2**" in report
    assert "buy & hold do BTC: **2/2**" in report
    assert "Resultado composto: **2.90%**" in report
    assert "cesta: **-4.12%**; BTC: **0.88%**" in report
    assert "Pior drawdown de janela: **4.50%**" in report
    assert "Parâmetros escolhidos" not in report
    empty = wf.summarize("x", [])
    assert "Resultado composto" not in empty
    assert "Parâmetros fixos" not in empty


def test_summarize_without_btc_data_and_with_chosen_params() -> None:
    results = [
        wf.WindowResult(
            date(2025, 1, 1), date(2025, 4, 1), 3, 1.0, 50.0, 1.0, 0.5, None, {"min_score": 0.45}
        ),
        wf.WindowResult(
            date(2025, 4, 1), date(2025, 7, 1), 2, 1.0, 50.0, 1.0, 0.5, None, {"adx_min": 25.0}
        ),
    ]
    report = wf.summarize("moderado", results)
    assert "| 1.00 | 0.50 | — | 0.50 |" in report
    assert "buy & hold do BTC: **0/0**" in report
    assert "BTC: **—%**" in report
    assert "| Janela | adx_min | min_score |" in report
    assert "| 2025-01-01 | — | 0.45 |" in report
    assert "| 2025-04-01 | 25 | — |" in report


@dataclass
class FakeLab:
    root: Path
    calls: list[tuple[str, ...]] = field(default_factory=list)
    backtest_params: list[dict[str, Any]] = field(default_factory=list)

    def reports(self, pattern: str) -> list[Path]:
        return list((self.root / "out").glob(pattern))


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeLab:
    """Lab isolated in ``tmp_path`` with a simulated Freqtrade (Docker)."""
    user_data = tmp_path / "user_data"
    (user_data / "strategies").mkdir(parents=True)
    (user_data / "config.base.json").write_text(json.dumps(BASE), encoding="utf-8")
    monkeypatch.setattr(wf, "USER_DATA", user_data)
    monkeypatch.setattr(wf, "LAB", tmp_path)
    monkeypatch.setattr(wf, "OUTPUT", tmp_path / "out")
    monkeypatch.setattr(wf, "DATA", tmp_path / "data")
    fake = FakeLab(tmp_path)
    params_file = user_data / "strategies" / wf.PARAMS_FILE

    def fake_freqtrade(*args: str) -> None:
        fake.calls.append(args)
        if args[0] == "hyperopt":  # Freqtrade exports only the optimized spaces
            params_file.write_text(json.dumps(_exported(tp_trailing=0.02)), encoding="utf-8")
        if args[0] == "backtesting":
            fake.backtest_params.append(json.loads(params_file.read_text(encoding="utf-8")))
            directory = tmp_path / args[args.index("--backtest-directory") + 1]
            (directory / "r.json").write_text(json.dumps(_stats()), encoding="utf-8")
            (directory / ".last_result.json").write_text(json.dumps({"latest_backtest": "r.json"}))

    monkeypatch.setattr(wf, "_freqtrade", fake_freqtrade)
    return fake


def test_main_with_fixed_profile_params(lab: FakeLab) -> None:
    code = wf.main(
        [
            "--profile", "conservador", "--start", "2025-01-01", "--end", "2025-07-01",
            "--download", "--profiles-file", str(PROFILES),
        ]
    )  # fmt: skip
    assert code == 0
    assert lab.calls[0][0] == "download-data"
    assert "20241001-20250701" in lab.calls[0]
    assert lab.calls[0].count("BTC/USDT") == 1  # reference pair without duplication
    assert [c[0] for c in lab.calls[1:]] == ["backtesting", "backtesting"]
    config = json.loads((lab.root / "user_data" / "config.conservador.json").read_text())
    assert config["timeframe"] == "4h"
    assert lab.backtest_params[0]["strategy_name"] == wf.STRATEGY
    assert lab.backtest_params[0] == lab.backtest_params[1]
    reports = lab.reports("walk-forward-conservador-fixo-*.md")
    assert len(reports) == 1
    assert "| — |" in reports[0].read_text(encoding="utf-8")  # no local BTC data


def test_main_optimizes_on_training_window(lab: FakeLab) -> None:
    (lab.root / "data").mkdir()
    jan1 = 1_735_689_600_000
    _jsongz(lab.root / "data" / "BTC_USDT-4h.json.gz", [[jan1, 100.0, 0, 0, 120.0, 1]])
    code = wf.main(
        [
            "--profile", "conservador", "--start", "2025-01-01", "--end", "2025-04-01",
            "--optimize", "--train-months", "6", "--epochs", "30", "--download",
            "--profiles-file", str(PROFILES),
        ]
    )  # fmt: skip
    assert code == 0
    assert "20240401-20250401" in lab.calls[0]  # training + warm-up
    hyperopt = lab.calls[1]
    assert hyperopt[0] == "hyperopt"
    assert "20240701-20250101" in hyperopt
    assert hyperopt[hyperopt.index("--epochs") + 1] == "30"
    spaces = hyperopt.index("--spaces")
    assert hyperopt[spaces + 1 : spaces + 3] == ("buy", "sell")
    applied = lab.backtest_params[0]["params"]
    assert applied["buy"]["min_score"] == 0.45
    assert applied["buy"]["risk_per_trade"] == pytest.approx(0.005)
    assert applied["sell"]["tp_trailing"] == 0.02
    text = lab.reports("walk-forward-conservador-otimizado-*.md")[0].read_text(encoding="utf-8")
    assert "Parâmetros otimizados (SharpeHyperOptLossDaily, 30 épocas) nos 6 meses" in text
    assert "| 20.00 |" in text  # BTC buy & hold computed from the local data
    assert "| 2025-01-01 | 25 | 0.45 | 0.07 | 0.02 |" in text


def test_optimize_writes_merged_params(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "strategies").mkdir()
    monkeypatch.setattr(wf, "USER_DATA", tmp_path)
    params_file = tmp_path / "strategies" / wf.PARAMS_FILE

    def fake_freqtrade(*args: str) -> None:
        assert json.loads(params_file.read_text())["params"]["buy"]["min_score"] == 0.5
        params_file.write_text(json.dumps(_exported()), encoding="utf-8")

    monkeypatch.setattr(wf, "_freqtrade", fake_freqtrade)
    args = argparse.Namespace(loss="L", epochs=5, seed=1, min_trades=3)
    chosen = wf.optimize(
        CONFIG.profiles["moderado"], "cfg", (date(2024, 1, 1), date(2025, 1, 1)), args
    )
    assert chosen["min_score"] == 0.45
    assert json.loads(params_file.read_text())["params"]["buy"]["risk_per_trade"] == 0.01


def test_freqtrade_invokes_docker_compose_with_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def flaky(cmd: list[str], check: bool) -> None:
        captured.append(cmd)
        if len(captured) < 2:
            raise wf.subprocess.CalledProcessError(2, cmd)  # type: ignore[attr-defined]

    monkeypatch.setattr("lab.walk_forward.subprocess.run", flaky)
    wf._freqtrade("backtesting", "--help")
    assert len(captured) == 2
    assert captured[0][:5] == ["docker", "compose", "-f", str(wf.COMPOSE), "run"]
    assert captured[0][-2:] == ["backtesting", "--help"]
    captured.clear()
    monkeypatch.setattr(
        "lab.walk_forward.subprocess.run",
        lambda cmd, check: (_ for _ in ()).throw(wf.subprocess.CalledProcessError(2, cmd)),  # type: ignore[attr-defined]
    )
    with pytest.raises(wf.subprocess.CalledProcessError):  # type: ignore[attr-defined]
        wf._freqtrade("backtesting", attempts=2)
