"""Technical signals: pure functions over OHLCV DataFrames.

This package is also imported by the lab's "shell" Freqtrade strategy
(``lab/freqtrade``), so it depends only on numpy, pandas and TA-Lib.
"""

from trade_agent.signals.features import (
    FeatureParams,
    LocalFeatureParams,
    compute_features,
    compute_local_features,
)
from trade_agent.signals.local import (
    BreakoutParams,
    Family,
    LocalParams,
    PullbackParams,
    RangeParams,
    evaluate_local,
    family_of,
    local_frame,
    position_exit,
)
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
    "BreakoutParams",
    "Family",
    "FeatureParams",
    "LocalFeatureParams",
    "LocalParams",
    "PullbackParams",
    "RangeParams",
    "Regime",
    "Signal",
    "SignalParams",
    "compute_features",
    "compute_local_features",
    "entry_mask",
    "evaluate",
    "evaluate_local",
    "exit_mask",
    "family_of",
    "local_frame",
    "market_regime",
    "position_exit",
    "score_frame",
    "setup_frame",
    "stop_pct_frame",
]
