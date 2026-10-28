"""Digest of everything the legacy profiles decide, for the bit-for-bit regression test (D-044).

Uses only the APIs that already existed before the pair-local families, so the same function
runs on the base commit's code: ``tests/fixtures/legacy_snapshot.json`` was written by it
there (``PYTHONPATH`` on that commit's ``src``) and the test recomputes it on this code. Each
entry hashes the raw bytes of a float column, the setup labels or the ``repr`` of a plan.
"""

import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tests.support.candles import (
    HOUR_MS,
    breakout,
    downtrend,
    ohlcv,
    rally_after_decline,
    uptrend_with_pullback,
)
from tests.support.exchange_info import rules_for
from trade_agent.market.universe import Tier, UniverseMember
from trade_agent.signals import (
    compute_features,
    entry_mask,
    evaluate,
    exit_mask,
    score_frame,
    setup_frame,
    stop_pct_frame,
)
from trade_agent.strategy.portfolio import Candidate, Holding, plan_entries
from trade_agent.strategy.profiles import load_strategy_config

PROFILES = ("swing_trend", "momentum_alpha", "alt_atr")
CONFIG = Path(__file__).parents[2] / "config" / "profiles.yaml"


def _walk(seed: int, n: int = 1200) -> pd.DataFrame:
    """A random walk with regimes and volume bursts (deterministic)."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0, 0.004, n // 100), 100)[:n]
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.012, n)))
    volumes = 100 * np.exp(rng.normal(0, 0.6, n))
    return ohlcv(closes, volumes=volumes, interval_ms=4 * HOUR_MS)


def _series() -> dict[str, tuple[pd.DataFrame, pd.Series | None]]:
    benchmark = _walk(1)["close"]
    rally = rally_after_decline(interval_ms=4 * HOUR_MS)
    return {
        "walk2": (_walk(2), benchmark),
        "walk3": (_walk(3), benchmark),
        "walk4_alone": (_walk(4), None),
        "pullback": (uptrend_with_pullback(), None),
        "breakout": (breakout(), None),
        "downtrend": (downtrend(), None),
        "rally": (rally, pd.Series(np.linspace(100, 90, len(rally)), index=rally.index)),
    }


def _digest(values: Any) -> str:
    return hashlib.sha256(np.ascontiguousarray(values).tobytes()).hexdigest()


def _labels(series: pd.Series) -> str:
    return hashlib.sha256("|".join(series.astype(str)).encode()).hexdigest()


def _plan_digest(profile_name: str) -> str:
    config = load_strategy_config(CONFIG)
    profile = config.profiles[profile_name]
    candidates = []
    for index, (symbol, tier) in enumerate(
        (("SOLUSDT", Tier.LARGE), ("ETHUSDT", Tier.CORE), ("ATOMUSDT", Tier.MID))
    ):
        rules = replace(rules_for("SOLUSDT"), symbol=symbol)
        member = UniverseMember(symbol, symbol[:-4], tier, 1, Decimal("1e8"), Decimal(1), rules)
        features = compute_features(_walk(10 + index))
        signal = replace(
            evaluate(features, profile.signal_params()), setup="trend_pullback", score=0.9
        )
        price = Decimal(str(round(signal.close, 2)))
        candidates.append(Candidate(member, signal, bid=price, ask=price))
    holdings = [Holding("other", "BNBUSDT", Tier.LARGE, Decimal(50))]
    plan = plan_entries(
        name=profile_name,
        profile=profile,
        capital=config.profile_capital(profile_name),
        holdings=holdings,
        candidates=candidates,
        execution_cost_pct=Decimal("0.3"),
    )
    return hashlib.sha256(repr(plan).encode()).hexdigest()


def snapshot() -> dict[str, str]:
    """``{profile/series/column: sha256}`` for every legacy profile."""
    config = load_strategy_config(CONFIG)
    result: dict[str, str] = {}
    for name in PROFILES:
        profile = config.profiles[name]
        params = profile.signal_params()
        for label, (frame, benchmark) in _series().items():
            features = compute_features(frame, benchmark_close=benchmark)
            key = f"{name}/{label}"
            result[f"{key}/features"] = _digest(features.astype("float64").to_numpy())
            result[f"{key}/score"] = _digest(score_frame(features, params).to_numpy())
            result[f"{key}/setup"] = _labels(setup_frame(features, params))
            result[f"{key}/stop"] = _digest(stop_pct_frame(features, params).to_numpy())
            entry = entry_mask(features, profile.entry.min_score, params)
            result[f"{key}/entry"] = _digest(entry.to_numpy())
            result[f"{key}/exit"] = _digest(
                exit_mask(features, profile.exits.exit_score).to_numpy()
            )
            s = evaluate(features, params)
            fields = (s.score, s.setup, s.stop_pct, s.atr_pct, s.close, s.stop_noise_setup)
            result[f"{key}/signal"] = hashlib.sha256(repr(fields).encode()).hexdigest()
        policy = profile.protection.policy(Decimal("3.2"), Decimal("1.4"))
        result[f"{name}/policy"] = hashlib.sha256(repr(policy).encode()).hexdigest()
        result[f"{name}/plan"] = _plan_digest(name)
    return result
