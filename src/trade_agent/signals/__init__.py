"""Technical signals: pure functions over OHLCV DataFrames.

This package is also imported by the lab's "shell" Freqtrade strategy
(``lab/freqtrade``), so it depends only on numpy, pandas and TA-Lib.
"""

from trade_agent.signals.features import FeatureParams, compute_features
from trade_agent.signals.setups import (
    Regime,
    Signal,
    SignalParams,
    entry_mask,
    evaluate,
    exit_mask,
    market_regime,
    score_frame,
    setup_frame,
    stop_pct_frame,
)

__all__ = [
    "FeatureParams",
    "Regime",
    "Signal",
    "SignalParams",
    "compute_features",
    "entry_mask",
    "evaluate",
    "exit_mask",
    "market_regime",
    "score_frame",
    "setup_frame",
    "stop_pct_frame",
]
