"""The lab report's computations (D-044): equity, drawdown, trade metrics, costs, bootstrap."""

import math
from typing import Any

import numpy as np
import pandas as pd
import pytest
from lab import report

T0 = pd.Timestamp("2025-01-01", tz="UTC")
H = pd.Timedelta("1h")


@pytest.fixture
def flat_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    """XYZ/USDT closes at 100 for 10 hours, then at 90."""
    index = pd.date_range(T0 - 5 * H, periods=40, freq="1h", tz="UTC")
    close = np.where(index < T0 + 10 * H, 100.0, 90.0)
    frame = pd.DataFrame({"open": close, "high": close, "low": close, "close": close}, index=index)
    monkeypatch.setattr(report, "candles", lambda pair, timeframe: frame)


def _trades(**columns: Any) -> pd.DataFrame:
    base: dict[str, Any] = {
        "pair": ["XYZ/USDT"],
        "opened": [T0 + 2 * H],
        "closed": [T0 + 20 * H],
        "amount": [1.0],
        "open_rate": [100.0],
        "close_rate": [90.0],
        "fee_open": [0.0],
        "fee_close": [0.0],
        "profit_abs": [-10.0],
        "stake_amount": [100.0],
        "risk": [5.0],
        "r": [-2.0],
        "mfe": [0.01],
        "mae": [-0.1],
        "given_back": [0.11],
        "fees": [0.0],
        "exit_reason": ["stop_loss"],
    }
    base.update(columns)
    return pd.DataFrame(base)


def test_equity_marks_open_positions_and_realizes_at_the_window_end(flat_candles: None) -> None:
    trades = _trades(closed=[T0 + 20 * H])
    equity = report.equity_curve(trades, 1000.0, "1h", T0, T0 + 20 * H)
    assert equity.loc[T0 + 2 * H] == 1000.0  # the mark of the candle closed at the entry
    assert equity.loc[T0 + 12 * H] == pytest.approx(990.0)  # marked at 90 while open
    assert equity.iloc[-1] == pytest.approx(990.0)  # realized at the window's end
    depth, longest = report.drawdown(equity)
    assert depth == pytest.approx(1.0) and longest == pd.Timedelta("9h")
    assert report.drawdown(equity.iloc[:0]) == (0.0, pd.Timedelta(0))


def test_trade_metrics_extra_costs_and_streaks() -> None:
    trades = _trades(
        pair=["A", "B", "C", "D"],
        opened=[T0, T0 + H, T0 + 2 * H, T0 + 3 * H],
        closed=[T0 + H, T0 + 2 * H, T0 + 3 * H, T0 + 4 * H],
        amount=[1.0] * 4,
        open_rate=[100.0] * 4,
        close_rate=[110.0, 95.0, 95.0, 100.0],
        profit_abs=[10.0, -5.0, -5.0, 0.0],
        risk=[5.0] * 4,
        r=[2.0, -1.0, -1.0, 0.0],
        mfe=[0.1] * 4,
        mae=[-0.05] * 4,
        given_back=[0.0] * 4,
        fees=[0.2] * 4,
        exit_reason=["roi", "stop_loss", "stop_loss", "max_holding"],
        stake_amount=[100.0] * 4,
        fee_open=[0.0] * 4,
        fee_close=[0.0] * 4,
    )
    metrics = report.trade_metrics(trades)
    assert (metrics["trades"], metrics["ties"], metrics["win_rate"]) == (4, 1, 25.0)
    assert metrics["payoff"] == 2.0 and metrics["profit_factor"] == 1.0
    assert metrics["expectancy"] == pytest.approx(0.25 * 10 - 0.5 * 5)
    assert metrics["max_losing_streak"] == 2
    costly = report.with_extra_cost(trades, 10)
    # 10 bps on each leg: 100 + 110 of notional costs 0.21
    assert costly["profit_abs"].iloc[0] == pytest.approx(10 - 0.21)
    assert costly["r"].iloc[0] == pytest.approx((10 - 0.21) / 5)
    assert report.with_extra_cost(trades, 0) is trades
    assert report.trade_metrics(trades.iloc[:0]) == {"trades": 0}
    assert report.concentration(trades)["best_pair"] == "A"


def test_bootstraps_are_deterministic_and_need_enough_data() -> None:
    daily = pd.Series(np.random.default_rng(1).normal(0.003, 0.01, 300))
    first, again = report.bootstrap(daily), report.bootstrap(daily)
    assert first == again
    assert first["return_p05"] < first["return_p50"] < first["return_p95"]
    assert 0 <= first["p_loss"] <= 1 and first["drawdown_p95"] >= first["drawdown_p50"]
    assert report.bootstrap(daily.iloc[:5]) == {}
    interval = report.expectancy_interval(pd.Series([1.0, -1.0] * 20))
    assert interval["r_p05"] <= 0 <= interval["r_p95"]
    assert report.expectancy_interval(pd.Series([1.0])) == {}
    sharpe, sortino = report.ratios(daily)
    assert sharpe > 0 and sortino > 0
    assert report.ratios(pd.Series([0.0, 0.0])) == (0.0, 0.0)


def _windowed(label: str, returns: list[float]) -> dict[str, Any]:
    return {"label": label, "windows": [{"return": r} for r in returns]}


def test_the_c_threshold_is_chosen_on_the_previous_year() -> None:
    windows = [
        _windowed("A-p", [1.0] * 6),
        _windowed("B-p", [0.0] * 6),
        _windowed("C0.52-p", [5.0, 5.0, 5.0, 5.0, -10.0, 0.0]),
        _windowed("C0.57-p", [0.0] * 6),
        _windowed("C0.62-p", [0.0] * 6),
        _windowed("C0.67-p", [1.0, 1.0, 1.0, 1.0, 2.0, 3.0]),
    ]
    ((profile, chosen, reselected, c67, b, a),) = report.threshold_walk_forward(windows)
    assert profile == "p" and chosen == "0.52 0.67"  # the fifth: 0.52 led; the sixth: 0.67
    assert reselected == pytest.approx((0.9 * 1.03 - 1) * 100)
    assert c67 == pytest.approx((1.02 * 1.03 - 1) * 100) and b == 0.0
    assert a == pytest.approx((1.01**2 - 1) * 100)
    assert report.threshold_walk_forward(windows[:2]) == []


def test_render_writes_every_table() -> None:
    windowed = {
        "label": "A-p", "extra_bps": 0, "trades": 1, "return": 1.0, "max_drawdown": 2.0,
        "win_rate": 100.0, "payoff": math.inf, "expectancy_r": 1.0, "profit_factor": math.inf,
        "segments": {"btc_down": {"trades": 1, "net": 1.0}}, "windows": [{"return": 1.0}],
        "concentration": {"pairs": 1, "best_pair": "X"}, "exits": {"roi": 1},
        "expectancy_interval": {},
    }  # fmt: skip
    continuous = {
        **windowed, "label": "D-p", "quarters": [{"start": "2025-01-01", "return": 1.5}],
        "bootstrap": {"return_p05": -1.0}, "events": {"scope: x": 2},
        "expectancy_interval": {"r_p05": -0.1, "r_p95": 0.3},
    }  # fmt: skip
    del continuous["windows"]
    text = report.render([windowed, continuous, {**continuous, "extra_bps": 5}])
    assert "| A-p | 1 | 1.00 | 2.00 | 100.00 | ∞ | 1.00 | ∞ | 1 | 1.00 |" in text
    assert "1.00 [-0.10; 0.30]" in text and "scope: x 2" in text and "| D-p | 1.50 |" in text
    assert report._n(None) == "—" and report._n(math.nan) == "—"
