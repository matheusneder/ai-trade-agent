# Laboratório — resultados do *walk-forward*

> Resultado da calibração dos perfis em uso (`swing_trend` e `momentum_alpha`, 01/10/2026). Os relatórios completos, janela a janela, ficam em `var/lab/` (não versionado).
> Resultados passados não garantem resultados futuros. Os números servem para **decidir se o agente deve operar** e **não são recomendação de investimento**.

## 1. Método

| Item | Valor |
|------|-------|
| Backtester | Freqtrade (`freqtradeorg/freqtrade:stable`) via Docker (D-014). A estratégia "casca" importa `trade_agent.signals`, o mesmo código de produção |
| Dados | Candles Spot públicos da Binance de 4h, desde 2021-10, para pares estáticos por *tier* (`lab/walk_forward.py`, `PAIRS_BY_TIER`): BTC e ETH; 10 *large*; 6 *mid*; 4 *small* |
| Janelas | 15 janelas trimestrais de validação, de 2023-01-01 a 2026-09-01 |
| Otimização (D-017) | Para cada janela, *hyperopt* (`SharpeHyperOptLossDaily`, 150 épocas, semente 42) nos **12 meses anteriores**. Os parâmetros escolhidos são aplicados **sem ajuste** na janela |
| Parâmetros otimizados | `min_score`, `adx_min`, `rsi_pullback_max`, `vol_rel_min`, `exit_score`, `atr_stop_mult`, `tp_activation`, `tp_trailing` |
| Fixos (do perfil) | Stop máximo, risco por trade, tamanho máximo e reserva de caixa. *Break-even* e prazo máximo não são simulados |
| Proteção simulada | Stop fixo `entrada × (1 − min(k·ATR%, stop_máx))` e *trailing take-profit* com ativação, como o OPOCO nativo |
| Taxas | 0,1% por lado (taxa padrão Spot, sem desconto BNB) |
| Referências | *Buy & hold* da cesta de pares do perfil (média simples) e do BTC |

```bash
uv run python -m lab.walk_forward --profile swing_trend --optimize --download
uv run python -m lab.walk_forward --profile momentum_alpha --optimize --download
uv run python -m lab.walk_forward --profile swing_trend          # parâmetros atuais do perfil, sem otimizar
```

O `--download` baixa os candles desde 15 meses antes da primeira janela (3 de aquecimento e 12 de treino). O `download-data` do Freqtrade não estende para trás um par que já tem dados. Para isso, rode-o uma vez com `--prepend`:

```bash
docker compose -f lab/freqtrade/docker-compose.yml run --rm freqtrade download-data \
    --config user_data/config.swing_trend.json --timerange 20211001- -t 4h --prepend
```

## 2. Resultados fora da amostra (01/10/2026)

| | `swing_trend` | `momentum_alpha` |
|---|---:|---:|
| Janelas com resultado positivo | 7/15 | 10/15 |
| Janelas acima do *buy & hold* da cesta | 8/15 | 8/15 |
| Janelas acima do *buy & hold* do BTC | 8/15 | 6/15 |
| **Resultado composto** | **+107,27%** | **+34,36%** |
| *Buy & hold* composto da cesta | +170,96% | +60,80% |
| *Buy & hold* composto do BTC | +375,05% | +375,05% |
| Trades | 316 | 301 |
| Pior drawdown de janela | 11,20% | 13,68% |

**Leitura:**

- **Expectativa positiva depois das taxas, fora da amostra**, nos dois perfis.
- **Retorno bem abaixo do *buy & hold***, com risco muito menor. Nas janelas negativas, as perdas foram de até −5% no `swing_trend` e de até −13% no `momentum_alpha` (2023-T3). Nessas janelas, a cesta caiu até −37% e −40%.
- **Concentração:** no `swing_trend`, o resultado vem principalmente de três trimestres de alta (2023-T4 +19,9%, 2024-T1 +14,1% e 2024-T4 +49,3%). O `momentum_alpha` distribui mais os ganhos (10 janelas positivas), mas com retorno total menor.
- **Estabilidade dos parâmetros:** a ativação do take-profit ficou entre 6% e 11,5% nos treinos dos dois perfis, um sinal consistente de deixar o lucro correr. Os parâmetros de entrada oscilam muito entre os treinos (`min_score` de 0,31 a 0,88; `adx_min` de 10 a 34), o que indica pouco poder preditivo dos filtros de entrada isolados.

## 3. Parâmetros adotados

`config/profiles.yaml` usa a **mediana dos 15 treinos** de cada perfil (D-028):

| Parâmetro | `swing_trend` | `momentum_alpha` |
|-----------|--------------:|-----------------:|
| `min_score` | 0,67 | 0,67 |
| `adx_min` | 24 | 29 |
| `rsi_pullback_max` | 41 | 35 |
| `vol_rel_min` | 2,3 | 2,6 |
| `exit_score` | −0,46 | −0,46 |
| `atr_mult` (stop) | 3,1 | 3,0 |
| `activation_pct` (TP) | 9,7 | 9,5 |
| `trailing_delta_bps` (TP) | 100 | 130 |

A mediana usa treinos posteriores às primeiras janelas, então esse conjunto **não** foi validado fora da amostra como um todo. O Demo (doc 04) é a validação que vale.

## 4. Limitações

- **Viés de sobrevivência:** a lista de pares é estática, formada por ativos que existem desde 2021. O universo real muda a cada ciclo (D-027), e a maior parte dos setups recentes apareceu em ativos fora dessa lista.
- **Execução idealizada:** preenchimento no preço do stop e na abertura do candle, sem *slippage* além da taxa. Em 4h, a ordem dos eventos dentro do candle é aproximada.
- **Sem a camada LLM**, sem os limites do Risk Guard, sem *break-even* e sem prazo máximo.
- **Amostra curta:** 15 janelas e cerca de 300 trades por perfil.

## 5. Histórico

Os primeiros testes (26/09/2026) usaram os perfis `conservador` (4h) e `moderado` (1h), já removidos. A linha de base com parâmetros fixos teve expectativa negativa nos dois perfis. As perdas iam ao stop cheio enquanto o trailing realizava ganhos pequenos, e havia compras de *altcoins* com o BTC em queda e rompimentos contra a tendência. Daí vieram o filtro de regime e os setups só a favor da tendência (D-016) e o *walk-forward* otimizado (D-017). Em 1h, o excesso de trades e as taxas tornaram a expectativa negativa mesmo com otimização, e os perfis atuais usam 4h.
