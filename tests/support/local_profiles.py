"""Pair-local profiles (D-044) for the tests, over the stable ``tests/fixtures/profiles.yaml``."""

import copy
from pathlib import Path
from typing import Any

import yaml

from trade_agent.strategy.profiles import ProfileConfig, StrategyConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "profiles.yaml"

LOCAL_PULLBACK: dict[str, Any] = {
    "code": "ppb",
    "enabled": False,
    "family": "pair_pullback_rsi",
    "capital_share": 0.10,
    "timeframe": "1h",
    "tiers": {"core": 0.70, "large": 0.70, "mid": 0.70, "small": 0.0},
    "allocation": {
        "max_open_positions": 2,
        "max_position_pct": 0.35,
        "cash_reserve_pct": 0.30,
        "risk_per_trade_pct": 0.5,
    },
    "entry": {"min_score": -1.0, "order": "limit_fok", "max_slippage_bps": 20},
    "protection": {
        "stop": {"mode": "fixed", "atr_mult": 1.5, "max_pct": 6.0},
        "take_profit": {"mode": "limit", "activation_pct": 0.5, "activation_r": 1.5},
        "max_holding": "48h",
    },
    "llm": {"enabled": False},
    "risk": {
        "cost_pct": 0.3,
        "max_open_risk_pct": 1.0,
        "daily_loss_pct": 1.5,
        "consecutive_losses": 3,
        "cooldown": "24h",
        "max_drawdown_pct": 8,
        "reentry_candles": 3,
    },
}


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def raw_config(**profiles: dict[str, Any]) -> dict[str, Any]:
    """The stable fixture plus ``profiles`` (name → overrides of :data:`LOCAL_PULLBACK`)."""
    data: dict[str, Any] = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    for name, overrides in profiles.items():
        data["profiles"][name] = merge(LOCAL_PULLBACK, overrides)
    return data


def local_config(**profiles: dict[str, Any]) -> StrategyConfig:
    return StrategyConfig.model_validate(raw_config(**profiles))


def local_profile(**overrides: Any) -> ProfileConfig:
    return local_config(local=overrides).profiles["local"]
