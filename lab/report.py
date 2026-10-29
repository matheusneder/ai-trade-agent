"""Metrics of the experiment runs (D-044, doc/lab-results.md section 2.10).

Reads the trades Freqtrade exported for each run of ``lab.experiments`` (``var/lab/runs``)
and the lab's candles, and computes, per run:

* the trades: win rate, ties, average win and loss, realized payoff, net expectancy in USDT
  and in R (R = the initial risk: the signal candle's stop, plus the profile's round-trip
  cost in the pair-local families), profit factor, MFE and MAE, profit given back, exit
  reasons, longest losing streak and the costs paid;
* the marked-to-market equity on the profile's candles (``unrealized = amount × close ×
  (1 − fee) − amount × entry × (1 + fee)``): total and annualized return, maximum drawdown
  and its recovery, daily Sharpe and Sortino (annualized with √365), exposure and turnover;
* each quarter of a continuous run, from that equity (positions cross the quarters as in the
  operation); a windowed run starts each window from the profile's capital, as Freqtrade did;
* the BTC regime at each entry (BTC's close against its EMA 200 on the profile's timeframe, on
  the last candle closed at the entry, as the agent saw it), the pair's ATR against its own
  median of the previous 500 candles, and the entries within 6 candles of a BTC regime flip,
  for auditing only: none of it gates the pair-local families;
* extra costs of 5, 10 and 20 bps per side, charged on both legs of every trade;
* a block bootstrap (10-day blocks of the daily returns, 2000 draws) of the total return and
  the maximum drawdown, and one of the expectancy (blocks of 5 consecutive trades);
* the concentration of the profit: by pair, and without the best pair and the 5 best trades.

Usage::

    uv run python -m lab.report --runs var/lab/runs --out var/lab/report-d044.json
"""

import argparse
import gzip
import itertools
import json
import math
import sys
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trade_agent.signals import (
    Family,
    SignalParams,
    compute_features,
    compute_local_features,
    local_frame,
    stop_pct_frame,
)
from trade_agent.signals.local import PARAMS as LOCAL_PARAMS
from trade_agent.signals.local import LocalParams

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "lab" / "freqtrade" / "user_data" / "data" / "binance"
RUNS = ROOT / "var" / "lab" / "runs"
EXTRA_COSTS_BPS = (0, 5, 10, 20)
BOOTSTRAP_DRAWS = 2000
DAY_BLOCK = 10
TRADE_BLOCK = 5
TRANSITION_CANDLES = 6
ATR_WINDOW = 500
QUARTERS = [
    *pd.date_range("2023-01-01", "2026-07-01", freq="QS", tz="UTC"),
    pd.Timestamp("2026-09-01", tz="UTC"),  # the last window is two months long
]
BENCHMARK = "BTC/USDT"
THRESHOLDS = (0.52, 0.57, 0.62, 0.67)
"""``lab.experiments.THRESHOLDS``: the ``min_score`` of the ``C`` runs."""


@dataclass(frozen=True)
class Run:
    label: str
    profile: dict[str, Any]
    """``trade_agent_profile.json`` of the run."""
    trades: pd.DataFrame
    windows: list[tuple[pd.Timestamp, pd.Timestamp, float]] = field(default_factory=list)
    """Each backtest's start, end and Freqtrade's maximum drawdown (closed trades)."""
    rejected: int = 0
    events: Counter[str] = field(default_factory=Counter)

    @property
    def continuous(self) -> bool:
        return len(self.windows) == 1


# ============================================================================ loading
def _result(archive: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive) as zipped:
        name = next(
            n
            for n in zipped.namelist()
            if n.endswith(".json")
            and "_config" not in n
            and "_market" not in n
            and not n.endswith("TradeAgentStrategy.json")
        )
        data: dict[str, Any] = json.loads(zipped.read(name))
    return data["strategy"]["TradeAgentStrategy"]


def load_run(directory: Path) -> Run:
    """A run of ``lab.experiments``: its manifest, trades and windows."""
    profile = json.loads(
        (directory / "user_data" / "strategies" / "trade_agent_profile.json").read_text(
            encoding="utf-8"
        )
    )
    trades: list[dict[str, Any]] = []
    windows: list[tuple[pd.Timestamp, pd.Timestamp, float]] = []
    rejected = 0
    for archive in sorted((directory / "user_data" / "backtest_results").glob("wf-*/*.zip")):
        stats = _result(archive)
        trades += stats["trades"]
        windows.append(
            (
                pd.Timestamp(stats["backtest_start"], tz="UTC"),
                pd.Timestamp(stats["backtest_end"], tz="UTC"),
                float(stats.get("max_drawdown_account", 0.0)),
            )
        )
        rejected += int(stats.get("rejected_signals", 0))
    frame = pd.DataFrame(trades)
    if not frame.empty:
        frame["opened"] = pd.to_datetime(frame["open_timestamp"], unit="ms", utc=True)
        frame["closed"] = pd.to_datetime(frame["close_timestamp"], unit="ms", utc=True)
        frame = frame.sort_values(["opened", "pair"]).reset_index(drop=True)
    return Run(directory.name, profile, frame, windows, rejected, _events(directory.name))


def _events(label: str) -> Counter[str]:
    """Scope pauses and refused entries the strategy logged (``trade_agent_events.jsonl``)."""
    counts: Counter[str] = Counter()
    pattern = f"walk-forward-*-{label}-[0-9]*.json"
    for manifest in (ROOT / "var" / "lab").glob(pattern):
        for window in json.loads(manifest.read_text(encoding="utf-8")).get("effective", []):
            for event in window.get("events", []):
                counts[f"{event['kind']}: {event.get('reason')}"] += 1
    return counts


@cache
def candles(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA / f"{pair.replace('/', '_')}-{timeframe}.json.gz"
    rows = json.loads(gzip.decompress(path.read_bytes()))
    frame = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    frame.index = pd.to_datetime(frame["t"], unit="ms", utc=True)
    return frame.drop(columns="t")


@cache
def _features(pair: str, timeframe: str, local: bool) -> pd.DataFrame:
    features = compute_features(candles(pair, timeframe))
    return compute_local_features(features) if local else features


def params_of(profile: dict[str, Any]) -> SignalParams | LocalParams:
    family = Family(profile["family"])
    if family.local:
        return LOCAL_PARAMS[family](**profile["signals"])
    return SignalParams(**profile["signals"])


@cache
def _btc_regime(timeframe: str) -> pd.DataFrame:
    features = compute_features(candles(BENCHMARK, timeframe))
    up = features["regime_up"].astype(bool)
    flip = up.ne(up.shift(1)) & features["ema_slow"].notna()
    recent = flip.astype(float).rolling(TRANSITION_CANDLES, min_periods=1).max() > 0
    return pd.DataFrame({"up": up, "transition": recent})


def _signal_row(index: pd.DatetimeIndex, opened: pd.Timestamp, span: pd.Timedelta) -> int | None:
    """Position of the last candle closed at ``opened`` (the entry's signal candle)."""
    position = int(index.searchsorted(opened - span, side="right")) - 1
    return position if position >= 0 else None


def enrich(run: Run) -> pd.DataFrame:
    """Adds the signal candle's stop, ATR%, R, MFE, MAE and the BTC regime to each trade."""
    trades = run.trades.copy()
    if trades.empty:
        return trades
    profile = run.profile
    timeframe = profile["timeframe"]
    span = pd.Timedelta(timeframe)
    params = params_of(profile)
    local = Family(profile["family"]).local
    regime = _btc_regime(timeframe)
    cost = float(profile["risk"]["cost_pct"]) / 100 if profile.get("risk") else 0.0
    stops, atrs, btc_up, transition, vol_high = [], [], [], [], []
    per_pair: dict[str, tuple[pd.Series, pd.Series, pd.Series]] = {}
    for pair, opened in zip(trades["pair"], trades["opened"], strict=True):
        features = _features(pair, timeframe, local)
        if pair not in per_pair:
            if isinstance(params, SignalParams):
                stop_series = stop_pct_frame(features, params)
            else:
                stop_series = local_frame(features, params)["stop_pct"]
            atr_series = features["atr_pct"]
            median_series = atr_series.rolling(ATR_WINDOW, min_periods=50).median()
            per_pair[pair] = (stop_series, atr_series, median_series)
        stop_series, atr, median = per_pair[pair]
        row = _signal_row(features.index, opened, span)
        stops.append(float(stop_series.iloc[row]) if row is not None else math.nan)
        atrs.append(float(atr.iloc[row]) if row is not None else math.nan)
        vol_high.append(bool(row is not None and atr.iloc[row] > median.iloc[row]))
        btc_row = _signal_row(regime.index, opened, span)
        btc_up.append(bool(regime["up"].iloc[btc_row]) if btc_row is not None else None)
        transition.append(
            bool(regime["transition"].iloc[btc_row]) if btc_row is not None else False
        )
    trades["stop_pct"] = stops
    trades["atr_pct"] = atrs
    trades["btc_up"] = btc_up
    trades["transition"] = transition
    trades["vol_high"] = vol_high
    trades["risk"] = trades["stake_amount"] * (trades["stop_pct"] + cost)
    trades["r"] = trades["profit_abs"] / trades["risk"]
    trades["mfe"] = trades["max_rate"] / trades["open_rate"] - 1
    trades["mae"] = trades["min_rate"] / trades["open_rate"] - 1
    trades["ret"] = trades["close_rate"] / trades["open_rate"] - 1
    trades["given_back"] = trades["mfe"] - trades["ret"]
    trades["fees"] = trades["amount"] * (
        trades["open_rate"] * trades["fee_open"] + trades["close_rate"] * trades["fee_close"]
    )
    return trades


def with_extra_cost(trades: pd.DataFrame, bps: float) -> pd.DataFrame:
    """The trades with ``bps`` more of cost per side, on both legs."""
    if not bps or trades.empty:
        return trades
    adjusted = trades.copy()
    notional = adjusted["amount"] * (adjusted["open_rate"] + adjusted["close_rate"])
    adjusted["profit_abs"] = adjusted["profit_abs"] - notional * bps / 10_000
    adjusted["r"] = adjusted["profit_abs"] / adjusted["risk"]
    return adjusted


# ============================================================================ equity
def equity_curve(
    trades: pd.DataFrame, capital: float, timeframe: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.Series:
    """Marked-to-market equity on the profile's candle closes from ``start`` to ``end``, both
    included: the trades the backtest closes at its end are realized there."""
    grid = pd.date_range(start, end, freq=timeframe, tz="UTC", inclusive="both")
    equity = pd.Series(capital, index=grid, dtype="float64")
    span = pd.Timedelta(timeframe)
    for trade in trades.itertuples():
        closes = candles(trade.pair, timeframe)["close"]
        # the value at each candle close while open: the candle opened one span before
        held = grid[(grid >= trade.opened) & (grid < trade.closed)]
        marks = closes.reindex(held - span).ffill().to_numpy()
        unrealized = trade.amount * marks * (1 - trade.fee_close) - trade.amount * (
            trade.open_rate * (1 + trade.fee_open)
        )
        equity.loc[held] += np.nan_to_num(unrealized)
        equity.loc[grid >= trade.closed] += trade.profit_abs
    return equity


def drawdown(equity: pd.Series) -> tuple[float, pd.Timedelta]:
    """Maximum drawdown (%) and the longest time below a previous peak."""
    if equity.empty:
        return 0.0, pd.Timedelta(0)
    peak = equity.cummax()
    depth = float(((peak - equity) / peak).max() * 100)
    under = equity < peak
    longest, start = pd.Timedelta(0), None
    for at, below in under.items():
        if below and start is None:
            start = at
        elif not below and start is not None:
            longest = max(longest, at - start)
            start = None
    if start is not None:
        longest = max(longest, equity.index[-1] - start)
    return depth, longest


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("1D").last().ffill().pct_change().dropna()


def ratios(daily: pd.Series) -> tuple[float, float]:
    """Sharpe and Sortino of the daily returns, annualized with √365 (no risk-free rate)."""
    if daily.empty or daily.std() == 0:
        return 0.0, 0.0
    sharpe = float(daily.mean() / daily.std() * math.sqrt(365))
    downside = daily[daily < 0]
    sortino = float(daily.mean() / downside.std() * math.sqrt(365)) if len(downside) > 1 else 0.0
    return sharpe, sortino


def bootstrap(daily: pd.Series, *, seed: int = 44) -> dict[str, float]:
    """Block bootstrap of the daily returns: percentiles of the total return and the drawdown."""
    values = daily.to_numpy()
    if len(values) < DAY_BLOCK * 2:
        return {}
    rng = np.random.default_rng(seed)
    blocks = math.ceil(len(values) / DAY_BLOCK)
    totals, drawdowns = [], []
    for _ in range(BOOTSTRAP_DRAWS):
        starts = rng.integers(0, len(values) - DAY_BLOCK + 1, blocks)
        sample = np.concatenate([values[s : s + DAY_BLOCK] for s in starts])[: len(values)]
        path = np.cumprod(1 + sample)
        totals.append((path[-1] - 1) * 100)
        peak = np.maximum.accumulate(np.concatenate([[1.0], path]))
        drawdowns.append(float(((peak[1:] - path) / peak[1:]).max() * 100))
    return {
        "return_p05": float(np.percentile(totals, 5)),
        "return_p50": float(np.percentile(totals, 50)),
        "return_p95": float(np.percentile(totals, 95)),
        "p_loss": float(np.mean(np.array(totals) <= 0)),
        "drawdown_p50": float(np.percentile(drawdowns, 50)),
        "drawdown_p95": float(np.percentile(drawdowns, 95)),
    }


def expectancy_interval(r: pd.Series, *, seed: int = 45) -> dict[str, float]:
    """Block bootstrap (consecutive trades) of the mean R per trade."""
    values = r.dropna().to_numpy()
    if len(values) < TRADE_BLOCK * 2:
        return {}
    rng = np.random.default_rng(seed)
    blocks = math.ceil(len(values) / TRADE_BLOCK)
    means = []
    for _ in range(BOOTSTRAP_DRAWS):
        starts = rng.integers(0, len(values) - TRADE_BLOCK + 1, blocks)
        sample = np.concatenate([values[s : s + TRADE_BLOCK] for s in starts])[: len(values)]
        means.append(sample.mean())
    return {
        "r_p05": float(np.percentile(means, 5)),
        "r_p95": float(np.percentile(means, 95)),
        "p_r_le_0": float(np.mean(np.array(means) <= 0)),
    }


# ============================================================================ metrics
def _streak(profits: pd.Series) -> int:
    longest = current = 0
    for profit in profits:
        current = current + 1 if profit < 0 else 0
        longest = max(longest, current)
    return longest


def trade_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    """The trade-level metrics of section 10 (empty dict without trades)."""
    if trades.empty:
        return {"trades": 0}
    profit = trades["profit_abs"]
    wins, losses = profit[profit > 0], profit[profit < 0]
    ties = int((profit == 0).sum())
    n = len(trades)
    p_win, p_loss = len(wins) / n, len(losses) / n
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    gross_loss = float(-losses.sum())
    return {
        "trades": n,
        "win_rate": p_win * 100,
        "ties": ties,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": avg_win / avg_loss if avg_loss else math.inf,
        "expectancy": p_win * avg_win - p_loss * avg_loss,
        "expectancy_r": float(trades["r"].mean()),
        "profit_factor": float(wins.sum()) / gross_loss if gross_loss else math.inf,
        "net": float(profit.sum()),
        "fees": float(trades["fees"].sum()),
        "mfe": float(trades["mfe"].mean() * 100),
        "mae": float(trades["mae"].mean() * 100),
        "given_back": float(trades["given_back"].mean() * 100),
        "max_losing_streak": _streak(profit),
        "hours": float((trades["closed"] - trades["opened"]).dt.total_seconds().mean() / 3600),
        "exits": dict(Counter(trades["exit_reason"])),
    }


def segments(trades: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """The trade metrics by BTC regime at the entry, transition and local volatility."""
    if trades.empty:
        return {}
    known = trades[trades["btc_up"].notna()]
    groups = {
        "btc_up": known[known["btc_up"].astype(bool)],
        "btc_down": known[~known["btc_up"].astype(bool)],
        "transition": trades[trades["transition"]],
        "vol_high": trades[trades["vol_high"]],
        "vol_low": trades[~trades["vol_high"]],
    }
    return {name: trade_metrics(group) for name, group in groups.items()}


def concentration(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {}
    by_pair = trades.groupby("pair")["profit_abs"].sum().sort_values(ascending=False)
    net = float(trades["profit_abs"].sum())
    best = by_pair.index[0]
    top5 = trades["profit_abs"].nlargest(5).sum()
    return {
        "pairs": len(by_pair),
        "best_pair": best,
        "best_pair_net": float(by_pair.iloc[0]),
        "net_without_best_pair": net - float(by_pair.iloc[0]),
        "net_without_top5_trades": net - float(top5),
        "by_pair": {pair: round(float(v), 2) for pair, v in by_pair.items()},
    }


def run_metrics(run: Run, extra_bps: float = 0) -> dict[str, Any]:
    """Every metric of a run, with ``extra_bps`` more of cost per side."""
    profile = run.profile
    capital = float(profile["capital"])
    timeframe = profile["timeframe"]
    trades = with_extra_cost(enrich(run), extra_bps)
    result: dict[str, Any] = {
        "label": run.label,
        "family": profile["family"],
        "timeframe": timeframe,
        "extra_bps": extra_bps,
        "capital": capital,
        "rejected": run.rejected,
        "events": dict(run.events),
        **trade_metrics(trades),
        "segments": segments(trades),
        "concentration": concentration(trades),
        "expectancy_interval": expectancy_interval(trades["r"]) if not trades.empty else {},
    }
    curves = []
    for start, end, _ in run.windows:
        inside = (
            trades[(trades["opened"] >= start) & (trades["opened"] < end)]
            if len(trades)
            else trades
        )
        curves.append(equity_curve(inside, capital, timeframe, start, end))
    if run.continuous:
        equity = curves[0]
        days = (equity.index[-1] - equity.index[0]).days or 1
        total = float(equity.iloc[-1] / capital - 1) * 100
        depth, recovery = drawdown(equity)
        daily = daily_returns(equity)
        sharpe, sortino = ratios(daily)
        held = (trades["closed"] - trades["opened"]).sum() if len(trades) else pd.Timedelta(0)
        notional = float((trades["stake_amount"]).sum()) if len(trades) else 0.0
        result |= {
            "return": total,
            "annualized": ((1 + total / 100) ** (365 / days) - 1) * 100,
            "max_drawdown": depth,
            "recovery_days": recovery.total_seconds() / 86400,
            "sharpe": sharpe,
            "sortino": sortino,
            "exposure": held / (equity.index[-1] - equity.index[0]) * 100,
            "turnover": notional / capital,
            "bootstrap": bootstrap(daily),
            "quarters": _quarters(equity),
        }
    else:
        returns = [float(c.iloc[-1] / capital - 1) * 100 for c in curves]
        result |= {
            "return": (math.prod(1 + r / 100 for r in returns) - 1) * 100,
            "max_drawdown": max(drawdown(c)[0] for c in curves),
            "windows": [
                {"start": str(s.date()), "return": r, "mtm_drawdown": drawdown(c)[0], "ft_dd": dd}
                for (s, _, dd), r, c in zip(run.windows, returns, curves, strict=True)
            ],
        }
    return result


def _quarters(equity: pd.Series) -> list[dict[str, Any]]:
    rows = []
    for start, end in itertools.pairwise(QUARTERS):
        part = equity[(equity.index >= start) & (equity.index < end)]
        if len(part) < 2:
            continue
        before = equity[equity.index < start]
        base = float(before.iloc[-1]) if len(before) else float(part.iloc[0])
        rows.append(
            {
                "start": str(start.date()),
                "return": float(part.iloc[-1] / base - 1) * 100,
                "mtm_drawdown": drawdown(pd.concat([pd.Series([base]), part]))[0],
            }
        )
    return rows


# ============================================================================ Markdown
def _n(value: Any, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if isinstance(value, float) and math.isinf(value):
        return "∞"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        cells = (v if isinstance(v, str) else _n(v) for v in row)
        lines.append("| " + " | ".join(cells) + " |")
    return [*lines, ""]


def _r_interval(m: dict[str, Any]) -> str:
    interval = m.get("expectancy_interval") or {}
    if not interval:
        return _n(m.get("expectancy_r"))
    return f"{_n(m.get('expectancy_r'))} [{_n(interval['r_p05'])}; {_n(interval['r_p95'])}]"


def render(results: Sequence[dict[str, Any]]) -> str:
    """The report's Markdown tables (Portuguese headers, like the other lab reports)."""
    base = {r["label"]: r for r in results if r["extra_bps"] == 0}
    costs = {(r["label"], r["extra_bps"]): r for r in results}
    windowed = [r for r in base.values() if "windows" in r]
    continuous = [r for r in base.values() if "quarters" in r]
    lines = ["# Experimentos D-044", ""]
    lines += ["## Walk-forward em janelas (A, B, C)", ""]
    lines += _table(
        ["Experimento", "Trades", "Composto %", "Pior DD MTM %", "Acerto %", "Payoff",
         "Expect. R [p05; p95]", "PF", "Trades BTC↓", "Líquido BTC↓"],
        [
            [r["label"], r["trades"], r.get("return"), r.get("max_drawdown"), r.get("win_rate"),
             r.get("payoff"), _r_interval(r), r.get("profit_factor"),
             r["segments"].get("btc_down", {}).get("trades", 0),
             r["segments"].get("btc_down", {}).get("net")]
            for r in windowed
        ],
    )  # fmt: skip
    lines += ["## Carteira contínua marcada a mercado (A_op, D, E)", ""]
    lines += _table(
        ["Experimento", "Trades", "Retorno %", "Anual %", "DD MTM %", "Recup. dias", "Sharpe",
         "Sortino", "Acerto %", "Payoff", "Expect. USDT", "Expect. R [p05; p95]", "PF",
         "Exposição %", "Giro", "+5 bps %", "+10 bps %", "+20 bps %"],
        [
            [r["label"], r["trades"], r.get("return"), r.get("annualized"), r.get("max_drawdown"),
             r.get("recovery_days"), r.get("sharpe"), r.get("sortino"), r.get("win_rate"),
             r.get("payoff"), r.get("expectancy"), _r_interval(r), r.get("profit_factor"),
             r.get("exposure"), r.get("turnover"),
             *(costs.get((r["label"], bps), {}).get("return") for bps in (5, 10, 20))]
            for r in continuous
        ],
    )  # fmt: skip
    lines += ["## Regime do BTC na entrada (auditoria; não decide nada nas famílias locais)", ""]
    lines += _table(
        ["Experimento", "BTC↑ trades", "BTC↑ líquido", "BTC↑ R", "BTC↓ trades", "BTC↓ líquido",
         "BTC↓ R", "Transição trades", "Transição líquido", "Vol. alta R", "Vol. baixa R"],
        [
            [r["label"], *(r["segments"].get(k, {}).get(f) for k in ("btc_up", "btc_down")
                           for f in ("trades", "net", "expectancy_r")),
             r["segments"].get("transition", {}).get("trades"),
             r["segments"].get("transition", {}).get("net"),
             r["segments"].get("vol_high", {}).get("expectancy_r"),
             r["segments"].get("vol_low", {}).get("expectancy_r")]
            for r in [*windowed, *continuous] if r["trades"]
        ],
    )  # fmt: skip
    lines += ["## Concentração, MFE/MAE e saídas", ""]
    lines += _table(
        ["Experimento", "Pares", "Melhor par", "Líquido", "Sem o melhor par", "Sem os 5 melhores",
         "MFE %", "MAE %", "Devolvido %", "Perdas seguidas", "Horas", "Taxas", "Saídas"],
        [
            [r["label"], r["concentration"].get("pairs"), str(r["concentration"].get("best_pair")),
             r.get("net"), r["concentration"].get("net_without_best_pair"),
             r["concentration"].get("net_without_top5_trades"), r.get("mfe"), r.get("mae"),
             r.get("given_back"), r.get("max_losing_streak"), r.get("hours"), r.get("fees"),
             ", ".join(f"{k} {v}" for k, v in sorted(r.get("exits", {}).items()))]
            for r in [*windowed, *continuous] if r["trades"]
        ],
    )  # fmt: skip
    lines += ["## Bootstrap por blocos de 10 dias (2000 sorteios)", ""]
    lines += _table(
        ["Experimento", "Retorno p05 %", "p50 %", "p95 %", "P(perda)", "DD p50 %", "DD p95 %",
         "Pausas e recusas do escopo"],
        [
            [r["label"], *(r.get("bootstrap", {}).get(k) for k in (
                "return_p05", "return_p50", "return_p95", "p_loss", "drawdown_p50",
                "drawdown_p95")),
             ", ".join(f"{k} {v}" for k, v in sorted(r.get("events", {}).items())) or "—"]
            for r in continuous
        ],
    )  # fmt: skip
    lines += ["## Trimestres da carteira contínua (retorno %)", ""]
    quarters = sorted({q["start"] for r in continuous for q in r["quarters"]})
    rows = []
    for r in continuous:
        by_quarter = {q["start"]: q["return"] for q in r["quarters"]}
        rows.append([r["label"], *(by_quarter.get(k) for k in quarters)])
    lines += _table(["Experimento", *quarters], rows)
    reselected = threshold_walk_forward(windowed)
    if reselected:
        lines += ["## C: limiar do score local reescolhido nos 12 meses anteriores", ""]
        lines += _table(
            ["Perfil", "Limiares escolhidos (janelas 5–15)", "C reescolhido %", "C 0.67 %",
             "B %", "A %"],
            reselected,
        )  # fmt: skip
    return "\n".join(lines)


TRAIN_WINDOWS = 4
"""C's re-selection: 12 months (4 quarterly windows) before each window."""


def _tail(run: dict[str, Any] | None, count: int) -> float | None:
    """Compounded return of the windows after the first training (5 to ``count``)."""
    if run is None:
        return None
    tail = [w["return"] for w in run["windows"]][TRAIN_WINDOWS:count]
    return (math.prod(1 + v / 100 for v in tail) - 1) * 100


def threshold_walk_forward(windowed: Sequence[dict[str, Any]]) -> list[list[Any]]:
    """For each profile and basket, the ``min_score`` of the ``C`` runs that compounded the most
    over the 4 windows before each window, applied to it (windows 5 to 15: the first 4 only
    train). Compared with ``C`` at 0.67, ``B`` and ``A`` over the same windows."""
    by_label = {r["label"]: r for r in windowed}
    rows = []
    for label in sorted(by_label):
        if not label.startswith("A-"):
            continue
        base = label.removeprefix("A-")
        runs = {t: by_label.get(f"C{t:g}-{base}") for t in THRESHOLDS}
        if any(r is None for r in runs.values()):
            continue
        returns = {t: [w["return"] for w in r["windows"]] for t, r in runs.items() if r}
        count = min(len(v) for v in returns.values())
        chosen, applied = [], []
        for i in range(TRAIN_WINDOWS, count):
            best = max(
                THRESHOLDS,
                key=lambda t: math.prod(1 + v / 100 for v in returns[t][i - TRAIN_WINDOWS : i]),
            )
            chosen.append(f"{best:g}")
            applied.append(returns[best][i])

        rows.append(
            [
                base, " ".join(chosen), (math.prod(1 + v / 100 for v in applied) - 1) * 100,
                _tail(runs[0.67], count), _tail(by_label.get(f"B-{base}"), count),
                _tail(by_label.get(label), count),
            ]
        )  # fmt: skip
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=Path, default=RUNS)
    parser.add_argument("--only", nargs="+", help="prefixos dos rótulos")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, help="tabelas em Markdown")
    parser.add_argument("--from-json", action="store_true", help="só refaz o Markdown do --out")
    args = parser.parse_args(argv)
    if args.from_json:
        cached = json.loads(args.out.read_text(encoding="utf-8"))["runs"]
        args.markdown.write_text(render(cached), encoding="utf-8")
        return 0
    results = []
    for directory in sorted(p for p in args.runs.iterdir() if p.is_dir()):
        if args.only and not directory.name.startswith(tuple(args.only)):
            continue
        if not list((directory / "user_data" / "backtest_results").glob("wf-*/*.zip")):
            continue
        run = load_run(directory)
        # the extra costs are reported for the continuous runs, the operation's capital model
        for bps in EXTRA_COSTS_BPS if run.continuous else (0,):
            results.append(run_metrics(run, bps))
        print(directory.name, flush=True)
    args.out.write_text(
        json.dumps({"generated": datetime.now(UTC).isoformat(), "runs": results}, default=str),
        encoding="utf-8",
    )
    if args.markdown:
        args.markdown.write_text(render(json.loads(args.out.read_text())["runs"]), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
