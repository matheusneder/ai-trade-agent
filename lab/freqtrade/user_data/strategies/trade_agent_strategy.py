"""Estratégia "casca" do laboratório: backtest/hyperopt dos sinais do agente no Freqtrade.

Importa o **mesmo** pacote de sinais usado em produção (``trade_agent.signals``, montado no
contêiner via ``PYTHONPATH``) e replica a proteção nativa do agente:

* stop fixo em ``entrada × (1 − stop_pct)``, com ``stop_pct`` do ATR limitado ao máximo do
  perfil (perna ``STOP_LOSS`` do OCO);
* *trailing take-profit*: ao atingir ``+tp_activation``, o stop passa a seguir o topo a
  ``tp_trailing`` de distância (perna ``TAKE_PROFIT`` com ``trailingDelta``);
* tamanho por risco: ``carteira × risco_por_trade / stop_pct``, limitado por posição.

Os parâmetros padrão são só um ponto de partida: ``lab/walk_forward.py`` gera o arquivo
``trade_agent_strategy.json`` com os valores do perfil escolhido em ``config/profiles.yaml``.
"""

from datetime import datetime
from typing import Any

from freqtrade.persistence import Trade
from freqtrade.strategy import (
    DecimalParameter,
    IStrategy,
    stoploss_from_open,
    timeframe_to_prev_date,
)
from pandas import DataFrame

from trade_agent.signals import (
    FeatureParams,
    SignalParams,
    compute_features,
    score_frame,
    setup_frame,
    stop_pct_frame,
)


class TradeAgentStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    minimal_roi = {"0": 100}  # noqa: RUF012 - sem ROI: saídas por stop, trailing TP e rotação
    stoploss = -0.25  # rede de segurança; o stop real vem de custom_stoploss
    use_custom_stoploss = True
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = FeatureParams().warmup
    benchmark_pair = "BTC/USDT"

    # ------------------------------------------------------------------ entrada (space=buy)
    min_score = DecimalParameter(0.2, 0.9, default=0.6, decimals=2, space="buy")
    adx_min = DecimalParameter(10, 35, default=20, decimals=0, space="buy")
    rsi_pullback_max = DecimalParameter(30, 55, default=45, decimals=0, space="buy")
    vol_rel_min = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="buy")
    risk_per_trade = DecimalParameter(
        0.001, 0.03, default=0.005, decimals=3, space="buy", optimize=False
    )
    max_position_pct = DecimalParameter(
        0.05, 1.0, default=0.25, decimals=2, space="buy", optimize=False
    )

    # ------------------------------------------------------------------ saída (space=sell)
    # Os parâmetros com optimize=False definem o risco do perfil e nunca são otimizados.
    atr_stop_mult = DecimalParameter(1.0, 4.0, default=2.0, decimals=1, space="sell")
    stop_max_pct = DecimalParameter(
        0.02, 0.10, default=0.04, decimals=3, space="sell", optimize=False
    )
    tp_activation = DecimalParameter(0.01, 0.12, default=0.03, decimals=3, space="sell")
    tp_trailing = DecimalParameter(0.005, 0.05, default=0.01, decimals=3, space="sell")
    exit_score = DecimalParameter(-0.8, 0.2, default=-0.2, decimals=2, space="sell")

    def informative_pairs(self) -> list[tuple[str, str]]:
        return [(self.benchmark_pair, self.timeframe)]

    def _signal_params(self) -> SignalParams:
        return SignalParams(
            adx_min=float(self.adx_min.value),
            rsi_pullback_max=float(self.rsi_pullback_max.value),
            vol_rel_min=float(self.vol_rel_min.value),
            atr_stop_mult=float(self.atr_stop_mult.value),
            max_stop_pct=float(self.stop_max_pct.value),
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        benchmark = None
        if metadata["pair"] != self.benchmark_pair and self.dp is not None:
            bench = self.dp.get_pair_dataframe(self.benchmark_pair, self.timeframe)
            if not bench.empty:
                benchmark = bench.set_index("date")["close"]
        features = compute_features(dataframe.set_index("date"), benchmark_close=benchmark)
        return features.reset_index()

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        # Score, setup e stop dependem de parâmetros otimizáveis: são calculados aqui porque o
        # hyperopt executa populate_indicators uma única vez e este método a cada época.
        params = self._signal_params()
        dataframe["score"] = score_frame(dataframe, params)
        dataframe["setup"] = setup_frame(dataframe, params)
        dataframe["stop_pct"] = stop_pct_frame(dataframe, params)
        entry = (
            (dataframe["setup"] != "")
            & (dataframe["score"] >= float(self.min_score.value))
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = dataframe.loc[entry, "setup"]
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        dataframe.loc[dataframe["score"] <= float(self.exit_score.value), "exit_long"] = 1
        return dataframe

    def _entry_stop_pct(self, pair: str, trade: Trade) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        candle = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        rows = dataframe.loc[dataframe["date"] == candle]
        if rows.empty:
            return float(self.stop_max_pct.value)
        return float(rows["stop_pct"].iloc[-1])

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        max_rate = trade.max_rate or trade.open_rate
        if max_rate >= trade.open_rate * (1 + float(self.tp_activation.value)):
            return -float(self.tp_trailing.value)  # trailing TP ativado: segue o topo
        stop_pct = self._entry_stop_pct(pair, trade)
        return stoploss_from_open(
            -stop_pct, current_profit, is_short=False, leverage=trade.leverage
        )

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stop_pct = (
            float(dataframe["stop_pct"].iloc[-1])
            if not dataframe.empty
            else float(self.stop_max_pct.value)
        )
        wallet = self.wallets.get_total_stake_amount()
        stake = wallet * float(self.risk_per_trade.value) / max(stop_pct, 1e-6)
        return min(stake, wallet * float(self.max_position_pct.value), max_stake)
