import argparse
import ast
import dataclasses
import gzip
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from lab import walk_forward as wf

from tests.support.local_profiles import local_profile, raw_config
from trade_agent.risk.conditions import PreTradeConfig, load_stop_conditions
from trade_agent.signals import BreakoutParams, PullbackParams, RangeParams, SignalParams
from trade_agent.strategy.profiles import StrategyConfig, load_strategy_config

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


def test_warmup_months_hold_the_startup_candles() -> None:
    """999 candles: 166.5 days on 4h, 41.6 on 1h, 999 on 1d (months of at least 28 days)."""
    assert (wf.warmup_months("4h"), wf.warmup_months("1h"), wf.warmup_months("1d")) == (6, 2, 36)


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
    custom = wf.lab_config(BASE, CONFIG.profiles["moderado"], Decimal("300"), ["SOL/USDT"])
    assert custom["exchange"]["pair_whitelist"] == ["SOL/USDT"]


def test_strategy_params_atr_take_profit_activation() -> None:
    data = yaml.safe_load(PROFILES.read_text(encoding="utf-8"))
    data["profiles"]["conservador"]["protection"]["take_profit"].update(
        {"activation_pct": 7, "activation_atr_mult": 5, "activation_max_pct": 16}
    )
    profile = StrategyConfig.model_validate(data).profiles["conservador"]
    sell = wf.strategy_params(profile)["params"]["sell"]
    assert sell["tp_activation"] == pytest.approx(0.07)
    assert sell["tp_activation_atr_mult"] == 5.0
    assert sell["tp_activation_max"] == pytest.approx(0.16)
    assert {"tp_activation_atr_mult", "tp_activation_max"} <= wf.RISK_PARAMS


def test_strategy_params_pre_trade_from_stop_conditions() -> None:
    pre_trade = load_stop_conditions(wf.ROOT / "config" / "stop_conditions.yaml").pre_trade
    buy = wf.strategy_params(CONFIG.profiles["conservador"], pre_trade)["params"]["buy"]
    assert buy["min_reward_risk"] == pre_trade.min_reward_risk
    assert buy["round_trip_fee"] == pytest.approx(pre_trade.round_trip_fee_pct / 100)
    assert {"min_reward_risk", "round_trip_fee"} <= wf.RISK_PARAMS


def test_strategy_params_min_stop_atr_and_universe_volume() -> None:
    data = yaml.safe_load(PROFILES.read_text(encoding="utf-8"))
    data["profiles"]["conservador"]["signals"] = {"min_stop_atr": 1.5}
    profile = StrategyConfig.model_validate(data).profiles["conservador"]
    buy = wf.strategy_params(profile, min_volume=5e6)["params"]["buy"]
    assert "min_stop_atr" not in buy  # in the manifest, with every signal parameter
    assert wf.profile_manifest(profile, Decimal(500))["signals"]["min_stop_atr"] == 1.5
    assert buy["universe_min_volume"] == 5e6
    off = wf.strategy_params(CONFIG.profiles["conservador"])["params"]["buy"]
    assert off["universe_min_volume"] == 0.0  # static basket
    assert "universe_min_volume" in wf.RISK_PARAMS


# Values different from every default, for each field of each family's parameters.
LEGACY_VALUES: dict[str, float | bool] = {
    "adx_min": 23.0,
    "rsi_pullback_max": 37.0,
    "rsi_overbought": 81.0,
    "vol_rel_min": 2.1,
    "min_stop_pct": 0.013,
    "use_regime_filter": False,
    "min_stop_atr": 1.7,
    "local_score": True,
}
LOCAL_VALUES: dict[str, dict[str, float | bool]] = {
    "pair_pullback_rsi": {
        "adx_min": 18.0, "rsi_dip": 38.0, "rsi_cross": 47.0, "rsi_max": 63.0,
        "max_extension_atr": 0.8, "max_drift_atr": 0.4, "vol_rel_min": 0.0,
        "obv_confirm": True, "stop_buffer_atr": 0.3, "exit_rsi": 43.0,
    },
    "pair_volatility_breakout": {
        "squeeze_pctile": 0.15, "rsi_min": 52.0, "rsi_max": 68.0, "vol_rel_min": 1.6,
        "max_extension_atr": 0.9, "max_range_atr": 2.2, "adx_filter": True, "adx_min": 22.0,
        "retest": True, "retest_tolerance_atr": 0.3, "stop_buffer_atr": 0.6,
    },
    "pair_range_reversion": {
        "adx_max": 18.0, "max_slope_atr": 0.2, "max_width_pctile": 0.7,
        "max_trend_gap_atr": 0.9, "rsi_cross": 32.0, "stop_buffer_atr": 0.3,
    },
}  # fmt: skip


def test_every_legacy_signal_parameter_reaches_the_strategy() -> None:
    """A profile option the lab dropped would test another strategy (D-044): the manifest
    carries every field, and the strategy rebuilds the very same parameters from it."""
    assert set(LEGACY_VALUES) == set(SignalParams.__dataclass_fields__) - {
        "atr_stop_mult",
        "max_stop_pct",
    }
    data = yaml.safe_load(PROFILES.read_text(encoding="utf-8"))
    data["profiles"]["conservador"]["signals"] = LEGACY_VALUES
    profile = StrategyConfig.model_validate(data).profiles["conservador"]
    manifest = json.loads(json.dumps(wf.profile_manifest(profile, Decimal(500))))
    params = wf.strategy_params(profile)
    rebuilt = SignalParams(**wf.expected_profile(params, manifest)["signals"])
    assert rebuilt == profile.signal_params()
    assert rebuilt.use_regime_filter is False and rebuilt.local_score is True


@pytest.mark.parametrize("family", sorted(LOCAL_VALUES))
def test_every_local_signal_parameter_reaches_the_strategy(family: str) -> None:
    kind: type[PullbackParams | BreakoutParams | RangeParams] = {
        "pair_pullback_rsi": PullbackParams,
        "pair_volatility_breakout": BreakoutParams,
        "pair_range_reversion": RangeParams,
    }[family]
    assert {f.name for f in dataclasses.fields(kind)} - {"min_stop_atr", "max_stop_pct"} == set(
        LOCAL_VALUES[family]
    )
    assert set(LOCAL_VALUES[family]) == {f.name for f in dataclasses.fields(kind)} - {
        "min_stop_atr",
        "max_stop_pct",
    }
    profile = local_profile(family=family, signals=LOCAL_VALUES[family])
    manifest = json.loads(json.dumps(wf.profile_manifest(profile, Decimal(100), "operational")))
    assert kind(**manifest["signals"]) == profile.local_params()
    assert wf.expected_profile(wf.strategy_params(profile), manifest) == manifest
    assert manifest["family"] == family and manifest["capital_model"] == "operational"


def test_profile_manifest_of_a_local_profile() -> None:
    profile = local_profile()
    manifest = wf.profile_manifest(profile, Decimal(100), "operational", PreTradeConfig())
    assert manifest["take_profit"] == {
        "mode": "limit",
        "activation_r": 1.5,
        "trailing_atr_mult": None,
        "signal_target": False,
    }
    assert manifest["risk"] == {
        "cost_pct": 0.3,
        "max_open_risk_pct": 1.0,
        "daily_loss_pct": 1.5,
        "consecutive_losses": 3,
        "cooldown_hours": 24.0,
        "max_drawdown_pct": 8.0,
        "reentry_candles": 3,
    }
    assert (manifest["capital"], manifest["cash_reserve"], manifest["timeframe"]) == (
        100.0,
        0.3,
        "1h",
    )
    bare = local_profile(
        risk={"max_open_risk_pct": None, "daily_loss_pct": None, "max_drawdown_pct": None}
    )
    loose = wf.profile_manifest(bare, Decimal(100))["risk"]
    assert (loose["max_open_risk_pct"], loose["daily_loss_pct"], loose["max_drawdown_pct"]) == (
        None,
        None,
        None,
    )
    assert wf.profile_manifest(CONFIG.profiles["conservador"], Decimal(500))["risk"] is None
    with pytest.raises(ValueError, match="modelo de capital"):
        wf.profile_manifest(profile, Decimal(100), "compounding")
    # the legacy parameters of a local profile are defaults, unused, with its stop
    sell = wf.strategy_params(profile)["params"]["sell"]
    assert (sell["atr_stop_mult"], sell["stop_max_pct"]) == (1.5, pytest.approx(0.06))


def _effective(params: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """What the strategy writes back when it used every value."""
    return {"params": params["params"], "profile": wf.expected_profile(params, manifest)}


def test_check_effective_refuses_any_value_the_strategy_did_not_use() -> None:
    profile = CONFIG.profiles["conservador"]
    params, manifest = wf.strategy_params(profile), wf.profile_manifest(profile, Decimal(500))
    wf.check_effective(params, manifest, _effective(params, manifest))
    rounded = _effective(params, manifest)
    rounded["params"] = {
        **params["params"],
        "buy": {**params["params"]["buy"], "risk_per_trade": 0.01},
    }
    with pytest.raises(ValueError, match=r"buy.risk_per_trade"):
        wf.check_effective(params, manifest, rounded)
    dropped = _effective(params, manifest)
    dropped["profile"] = {
        **dropped["profile"],
        "signals": {**manifest["signals"], "local_score": True},
    }
    with pytest.raises(ValueError, match="signals"):
        wf.check_effective(params, manifest, dropped)
    # hyperopt's choice comes from Freqtrade's parameters, not from the profile
    tuned = json.loads(json.dumps(params))
    tuned["params"]["buy"]["adx_min"] = 31
    assert wf.expected_profile(tuned, manifest)["signals"]["adx_min"] == 31.0
    wf.check_effective(tuned, manifest, _effective(tuned, manifest))


def test_universe_pairs_with_alts() -> None:
    profile = CONFIG.profiles["conservador"]
    assert wf.universe_pairs(profile) == (None, 0.0)
    assert wf.universe_pairs(profile, ["SOL/USDT"]) == (["SOL/USDT"], 0.0)
    pairs, volume = wf.universe_pairs(profile, alts=True)
    assert pairs is not None
    assert pairs[: len(wf.profile_pairs(profile))] == wf.profile_pairs(profile)
    assert set(wf.ALTS) <= set(pairs)
    assert volume == 5_000_000  # UniverseConfig.min_quote_volume_24h
    custom, _ = wf.universe_pairs(profile, ["SAND/USDT"], alts=True)
    assert custom is not None
    assert custom[0] == "SAND/USDT"
    assert custom.count("SAND/USDT") == 1


def test_detail_args() -> None:
    assert wf.detail_args("5m") == ["--timeframe-detail", "5m"]
    assert wf.detail_args(None) == []


def test_strategy_params_fixed_and_trailing_stop() -> None:
    fixed = wf.strategy_params(CONFIG.profiles["conservador"])["params"]
    assert fixed["buy"]["min_score"] == 0.6
    assert fixed["buy"]["risk_per_trade"] == pytest.approx(0.005)
    assert fixed["buy"]["min_reward_risk"] == 1.5  # PreTradeConfig defaults
    assert fixed["buy"]["round_trip_fee"] == pytest.approx(0.002)
    assert fixed["sell"]["stop_max_pct"] == pytest.approx(0.04)
    assert fixed["sell"]["tp_activation"] == pytest.approx(0.03)
    assert fixed["sell"]["tp_trailing"] == pytest.approx(0.01)
    assert fixed["sell"]["tp_activation_atr_mult"] == 0.0  # ATR-based activation off
    assert fixed["sell"]["tp_activation_max"] == 0.0
    assert fixed["sell"]["atr_stop_mult"] == 2.0
    assert fixed["sell"]["break_even_r"] == 1.0
    assert fixed["sell"]["max_holding_hours"] == 21 * 24
    trailing = wf.strategy_params(CONFIG.profiles["agressivo"])["params"]["sell"]
    assert trailing["stop_max_pct"] == pytest.approx(0.08)
    assert trailing["atr_stop_mult"] == 100.0
    assert trailing["break_even_r"] == 0.0  # no break-even in the profile
    assert trailing["max_holding_hours"] == 7 * 24


def _exported(**sell: float) -> dict[str, object]:
    return {
        "strategy_name": wf.STRATEGY,
        "params": {
            # Freqtrade also exports the non-optimized ones; the risk ones must be ignored
            "buy": {"min_score": 0.45, "adx_min": 25, "risk_per_trade": 0.02},
            "sell": {"tp_activation": 0.07, "break_even_r": 3.0, **sell},
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
    assert merged["params"]["sell"]["break_even_r"] == 1.0  # decision exits from the profile
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
    events: str = ""
    """What the strategy logs to its events file on each backtest."""

    def reports(self, pattern: str) -> list[Path]:
        return list((self.root / "out").glob(pattern))


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeLab:
    """Lab isolated in ``tmp_path`` with a simulated Freqtrade (Docker)."""
    user_data = tmp_path / "user_data"
    strategies = user_data / "strategies"
    strategies.mkdir(parents=True)
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
            params = json.loads(params_file.read_text(encoding="utf-8"))
            fake.backtest_params.append(params)
            manifest = json.loads((strategies / wf.PROFILE_FILE).read_text(encoding="utf-8"))
            effective = _effective(params, manifest)
            (strategies / wf.EFFECTIVE_FILE).write_text(json.dumps(effective), encoding="utf-8")
            (strategies / wf.EVENTS_FILE).write_text(fake.events, encoding="utf-8")
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
    assert "5m" in lab.calls[0]
    assert all("--timeframe-detail" in call for call in lab.calls[1:])
    assert "20240701-20250701" in lab.calls[0]  # 6 months for the 999 startup candles
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
    assert "20240101-20250401" in lab.calls[0]  # training + warm-up
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


def test_main_with_custom_pairs_and_candle_detail(lab: FakeLab) -> None:
    code = wf.main(
        [
            "--profile", "conservador", "--start", "2025-01-01", "--end", "2025-04-01",
            "--optimize", "--epochs", "5", "--download", "--pairs", "SOL/USDT", "ETH/USDT",
            "--detail", "5m", "--profiles-file", str(PROFILES),
        ]
    )  # fmt: skip
    assert code == 0
    download = lab.calls[0]
    assert download[download.index("-t") + 1 : download.index("-t") + 3] == ("4h", "5m")
    assert download[download.index("--pairs") + 1 :] == ("SOL/USDT", "ETH/USDT", "BTC/USDT")
    for call in lab.calls[1:]:  # hyperopt and backtesting
        assert call[call.index("--timeframe-detail") + 1] == "5m"
    config = json.loads((lab.root / "user_data" / "config.conservador.json").read_text())
    assert config["exchange"]["pair_whitelist"] == ["SOL/USDT", "ETH/USDT"]
    reports = lab.reports("walk-forward-conservador-otimizado-detalhe5m-pares-*.md")
    text = reports[0].read_text(encoding="utf-8")
    assert "Saídas simuladas dentro do candle com candles de 5m. Pares: SOL/USDT, ETH/USDT." in text


def test_main_with_alts_emulates_the_universe(lab: FakeLab) -> None:
    code = wf.main(
        [
            "--profile", "conservador", "--start", "2025-01-01", "--end", "2025-04-01",
            "--alts", "--download", "--profiles-file", str(PROFILES),
        ]
    )  # fmt: skip
    assert code == 0
    assert "MOVR/USDT" in lab.calls[0]
    assert lab.calls[0].count("BTC/USDT") == 1
    assert lab.backtest_params[0]["params"]["buy"]["universe_min_volume"] == 5_000_000
    config = json.loads((lab.root / "user_data" / "config.conservador.json").read_text())
    assert set(wf.ALTS) <= set(config["exchange"]["pair_whitelist"])
    text = lab.reports("walk-forward-conservador-fixo-detalhe5m-alts-*.md")[0].read_text(
        encoding="utf-8"
    )
    assert f"Com {len(wf.ALTS)} alts voláteis" in text
    assert "≥ 5 mi USDT" in text


def test_no_detail_requires_explicit_exploratory_mode(lab: FakeLab) -> None:
    wf.main(
        [
            "--profile",
            "conservador",
            "--start",
            "2025-01-01",
            "--end",
            "2025-04-01",
            "--profiles-file",
            str(PROFILES),
            "--exploratory-no-detail",
        ]
    )
    assert "--timeframe-detail" not in lab.calls[0]
    report = lab.reports("*.md")[0].read_text(encoding="utf-8")
    assert "EXPLORATÓRIO" in report


def test_optimize_writes_merged_params(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "strategies").mkdir()
    monkeypatch.setattr(wf, "USER_DATA", tmp_path)
    params_file = tmp_path / "strategies" / wf.PARAMS_FILE

    def fake_freqtrade(*args: str) -> None:
        assert json.loads(params_file.read_text())["params"]["buy"]["min_score"] == 0.5
        params_file.write_text(json.dumps(_exported()), encoding="utf-8")

    monkeypatch.setattr(wf, "_freqtrade", fake_freqtrade)
    args = argparse.Namespace(
        loss="L",
        epochs=5,
        seed=1,
        min_trades=3,
        detail=None,
        stop_conditions=wf.ROOT / "config" / "stop_conditions.yaml",
        pairs=None,
        alts=True,
    )
    chosen = wf.optimize(
        CONFIG.profiles["moderado"], "cfg", (date(2024, 1, 1), date(2025, 1, 1)), args
    )
    assert chosen["min_score"] == 0.45
    merged = json.loads(params_file.read_text())["params"]["buy"]
    assert merged["risk_per_trade"] == 0.01
    assert merged["universe_min_volume"] == 5_000_000  # --alts also during the training


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


def test_main_records_the_effective_values_and_the_events(lab: FakeLab) -> None:
    lab.events = '{"kind": "scope", "reason": "perdas seguidas do perfil"}\n'
    profiles = lab.root / "profiles.yaml"
    profiles.write_text(yaml.safe_dump(_local_profiles()), encoding="utf-8")
    code = wf.main(
        [
            "--profile", "local", "--start", "2025-01-01", "--end", "2025-04-01",
            "--profiles-file", str(profiles), "--capital-model", "operational",
            "--fee", "0.0015", "--label", "D-x",
        ]
    )  # fmt: skip
    assert code == 0
    (backtest,) = lab.calls
    assert backtest[backtest.index("--fee") + 1] == "0.0015"
    (report,) = lab.reports("walk-forward-local-fixo-detalhe5m-capital-fee0.0015-D-x-*.md")
    text = report.read_text(encoding="utf-8")
    assert "sem reinvestir o resultado" in text and "Taxa de 0.15% por lado." in text
    manifest = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    (window,) = manifest["effective"]
    assert window["profile"]["family"] == "pair_pullback_rsi"
    assert window["events"] == [{"kind": "scope", "reason": "perdas seguidas do perfil"}]


def test_main_stops_on_an_error_the_strategy_logged(lab: FakeLab) -> None:
    lab.events = '{"kind": "error", "callback": "custom_roi", "error": "KeyError()"}\n'
    with pytest.raises(RuntimeError, match="custom_roi"):
        wf.main(
            [
                "--profile", "conservador", "--start", "2025-01-01", "--end", "2025-04-01",
                "--profiles-file", str(PROFILES),
            ]
        )  # fmt: skip


def test_main_without_an_events_file(lab: FakeLab, monkeypatch: pytest.MonkeyPatch) -> None:
    lab.events = ""
    original = wf._read_effective

    def without_events() -> dict[str, Any]:
        (wf.USER_DATA / "strategies" / wf.EVENTS_FILE).unlink()
        return original()

    monkeypatch.setattr(wf, "_read_effective", without_events)
    args = ["--profile", "conservador", "--start", "2025-01-01", "--end", "2025-04-01"]
    assert wf.main([*args, "--profiles-file", str(PROFILES)]) == 0


def _local_profiles() -> dict[str, Any]:
    data = raw_config(local={})
    return {"account": data["account"], "profiles": {"local": data["profiles"]["local"]}}


def test_the_lab_strategy_keeps_the_after_fill_signature() -> None:
    """Freqtrade calls ``custom_stoploss`` right after the fill only when ``after_fill`` is in
    its ``getfullargspec`` (which ignores ``functools.wraps``): a decorator there labelled
    every stop ``trailing_stop_loss`` and turned off the re-entry wait after a stop (D-044)."""
    source = (wf.USER_DATA / "strategies" / "trade_agent_strategy.py").read_text(encoding="utf-8")
    (strategy,) = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == wf.STRATEGY
    ]
    (stoploss,) = [
        node
        for node in strategy.body
        if isinstance(node, ast.FunctionDef) and node.name == "custom_stoploss"
    ]
    assert stoploss.decorator_list == []
    assert "after_fill" in [a.arg for a in stoploss.args.args]
