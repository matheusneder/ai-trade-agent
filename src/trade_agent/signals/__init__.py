"""Sinais técnicos: funções puras sobre DataFrames OHLCV.

Este pacote também é importado pela estratégia "casca" do laboratório Freqtrade
(``lab/freqtrade``), por isso depende apenas de numpy, pandas e TA-Lib.
"""

from trade_agent.signals.features import FeatureParams, compute_features
from trade_agent.signals.setups import (
    Signal,
    SignalParams,
    entry_mask,
    evaluate,
    exit_mask,
    score_frame,
    setup_frame,
    stop_pct_frame,
)

__all__ = [
    "FeatureParams",
    "Signal",
    "SignalParams",
    "compute_features",
    "entry_mask",
    "evaluate",
    "exit_mask",
    "score_frame",
    "setup_frame",
    "stop_pct_frame",
]
