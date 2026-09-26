# Laboratório — resultados do *walk-forward* (Fase 3)

> Registro honesto do critério de saída da Fase 3 (doc 04): *"backtest walk-forward dos setups com expectativa positiva após taxas nos períodos de validação e comparado ao BTC buy & hold"*.
> Resultados passados não garantem resultados futuros. Os números abaixo servem para **decidir se o agente deve operar** e **não são recomendação de investimento**.

## 1. Como reproduzir

| Item | Valor |
|------|-------|
| Backtester | Freqtrade `stable` (2026.8) via Docker (D-014); a estratégia "casca" importa `trade_agent.signals` |
| Dados | Candles Spot públicos da Binance, pares estáticos por *tier* (`lab/walk_forward.py`, `PAIRS_BY_TIER`) |
| Taxas | 0,1% por lado (taxa padrão Spot, sem desconto BNB) |
| Proteção simulada | Stop fixo `entrada × (1 − min(k·ATR%, stop_máx))` + *trailing take-profit* com ativação (equivalente ao OPOCO/OCO nativo) |
| Tamanho | Risco por trade ÷ distância do stop, limitado por `max_position_pct` e pela reserva de caixa |
| Referências | *Buy & hold* da cesta de pares (média simples, calculada pelo Freqtrade) e do BTC |

```bash
uv run python -m lab.walk_forward --profile conservador --start 2023-01-01 --download        # parâmetros fixos
uv run python -m lab.walk_forward --profile conservador --start 2024-01-01 --optimize --epochs 100
```

Os relatórios completos, janela a janela, ficam em `var/lab/`.

## 2. Iteração 1 — baseline (sinais v1, parâmetros ilustrativos do perfil)

Janelas trimestrais de 2023-01-01 a 2026-09-01 (15 janelas), parâmetros fixos de `config/profiles.yaml`:

| Perfil | Timeframe | Janelas positivas | Acima do B&H da cesta | Resultado composto | Trades | Pior DD de janela |
|--------|-----------|------------------:|----------------------:|-------------------:|-------:|------------------:|
| conservador | 4h | 1/15 | 7/15 | **−44,88%** | 1.188 | 15,77% |
| moderado | 1h | 0/15 | 5/15 | **−95,60%** | 4.179 | 40,29% |

No mesmo período, o *buy & hold* composto rendeu +171% (cesta do conservador) e +375% (BTC).

**Diagnóstico** (saídas agregadas do conservador):

| Motivo de saída | Trades | Média por trade | Acerto |
|-----------------|-------:|----------------:|-------:|
| Stop (−4% máx.) | 309 | −3,99% | 0% |
| *Trailing* (stop ajustado / TP) | 837 | +0,71% | 49% |
| Sinal de saída | 25 | −1,52% | 4% |

1. **Payoff invertido:** as perdas chegam ao stop cheio (≈ −4%), enquanto o *trailing* de 1% ativado em +3% realiza ganhos de ≈ +2% no melhor caso. Com 35% de acerto, a expectativa é negativa.
2. **Rompimentos ruidosos:** 957 dos 1.188 trades foram `breakout`, com média de −0,60%, inclusive contra a tendência.
3. **Sem filtro de regime:** compras de *altcoins* com o BTC em queda. As *alts* tiveram o pior resultado por par.
4. **Moderado (1h):** excesso de trades (≈ 280 por trimestre). As taxas e as saídas por sinal (962 trades, média de −1,37%) destroem o resultado.

## 3. Iteração 2 — sinais v2 (D-016)

Mudanças definidas **antes** de qualquer otimização: setups só a favor da tendência (EMA 50 > EMA 200, também no rompimento) e filtro de regime (BTC acima da própria EMA 200).

Com os mesmos parâmetros fixos, o conservador passou de −44,88% para **−41,02%** (1.052 trades): 1/15 janelas positivas, 8/15 acima da cesta e 6/15 acima do BTC. O filtro reduziu pouco o número de trades porque o BTC passou a maior parte do período em alta. **O gargalo é o payoff da proteção, não a entrada.** Parâmetros de take-profit e de sinal precisam ser calibrados, e isso só é honesto com otimização fora da amostra.

## 4. Iteração 2 — *walk-forward* otimizado (D-017)

Para cada janela trimestral de validação (2024-01-01 a 2026-09-01, 11 janelas), o *hyperopt* (`SharpeHyperOptLossDaily`, 100 épocas, semente 42) escolhe os parâmetros de sinal e de take-profit nos **12 meses anteriores**. Esses parâmetros são aplicados **sem ajuste** na janela. Stop máximo, risco por trade e tamanho máximo continuam sendo os do perfil.

### 4.1 Conservador (4h, BTC/ETH + 10 *large caps*)

| Janela | Trades | Resultado % | Cesta B&H % | BTC B&H % | Acerto % | DD máx % |
|--------|-------:|------------:|------------:|----------:|---------:|---------:|
| 2024-01 → 2024-04 | 80 | +10,28 | +51,75 | +68,58 | 35,0 | 7,55 |
| 2024-04 → 2024-07 | 17 | −3,01 | −23,34 | −11,94 | 11,8 | 3,01 |
| 2024-07 → 2024-10 | 3 | −0,79 | −2,96 | +0,89 | 0,0 | 0,79 |
| 2024-10 → 2025-01 | 78 | +8,47 | +75,64 | +47,76 | 30,8 | 5,78 |
| 2025-01 → 2025-04 | 10 | −2,05 | −26,51 | −11,78 | 10,0 | 2,76 |
| 2025-04 → 2025-07 | 13 | +1,07 | +6,67 | +29,80 | 30,8 | 1,39 |
| 2025-07 → 2025-10 | 19 | −1,20 | +37,33 | +6,44 | 26,3 | 2,62 |
| 2025-10 → 2026-01 | 4 | −0,11 | −36,94 | −23,15 | 25,0 | 0,69 |
| 2026-01 → 2026-04 | 10 | −2,58 | −24,24 | −22,09 | 10,0 | 2,58 |
| 2026-04 → 2026-07 | 15 | −0,55 | −19,76 | −14,15 | 33,3 | 1,87 |
| 2026-07 → 2026-09 | 19 | +2,30 | +26,63 | +34,04 | 42,1 | 2,76 |
| **Composto** | **268** | **+11,47** | **+3,62** | **+85,84** | | **7,55** (pior janela) |

| Motivo de saída | Trades | Média por trade | Acerto |
|-----------------|-------:|----------------:|-------:|
| Stop (−4% máx.) | 146 | −3,99% | 0% |
| *Trailing take-profit* ou stop por ATR (< 4%) | 113 | **+6,33%** | 67% |
| Sinal de saída / fim do período | 9 | −0,84% | 33% |

**Leitura:**

- **Expectativa positiva após taxas fora da amostra:** ≈ +0,46% por trade e +11,47% compostos em 32 meses. O payoff se inverteu em relação ao baseline: ganhos médios de +6,3% contra perdas de −4,0%, com ~30% de acerto (perfil típico de seguidor de tendência).
- **Concentração:** o resultado vem de dois trimestres de alta forte (2024-T1 e 2024-T4). Nas outras janelas, as perdas foram pequenas, entre −0,1% e −3%, e a exposição caiu sozinha nas quedas (3 a 19 trades por trimestre).
- **Contra o *buy & hold*:** o BTC rendeu muito mais no período (+85,8%), com quedas trimestrais de até −23%. A estratégia ficou com retorno menor e risco muito menor: pior drawdown de janela de 7,55%, e 8 de 11 janelas dentro de ±3%. Nas 5 janelas de queda do BTC, a perda máxima foi de −3,0%.
- **Estabilidade dos parâmetros:** `tp_activation` ficou entre 10% e 11,5% em 8 de 11 treinos, e `atr_stop_mult`, entre 2 e 3,5. Esses são sinais robustos: deixar o lucro correr e usar um stop mais largo. Os parâmetros de entrada (`adx_min` de 10 a 34, `min_score` de 0,23 a 0,77) oscilam muito, o que indica pouco poder preditivo dos filtros de entrada isolados.

### 4.2 Moderado (1h, BTC/ETH + *large* + *mid caps*)

| Janela | Trades | Resultado % | Cesta B&H % | BTC B&H % | Acerto % | DD máx % |
|--------|-------:|------------:|------------:|----------:|---------:|---------:|
| 2024-01 → 2024-04 | 223 | −5,42 | +51,09 | +68,58 | 45,3 | 13,10 |
| 2024-04 → 2024-07 | 96 | −19,89 | −30,65 | −11,94 | 11,5 | 19,89 |
| 2024-07 → 2024-10 | 99 | −1,07 | −4,94 | +0,89 | 26,3 | 7,87 |
| 2024-10 → 2025-01 | 153 | +8,67 | +60,31 | +47,76 | 40,5 | 12,56 |
| 2025-01 → 2025-04 | 6 | −0,28 | −33,71 | −11,78 | 50,0 | 0,89 |
| 2025-04 → 2025-07 | 10 | −4,50 | +2,23 | +29,80 | 10,0 | 5,44 |
| 2025-07 → 2025-10 | 97 | +21,85 | +26,95 | +6,44 | 32,0 | 5,14 |
| 2025-10 → 2026-01 | 50 | −2,12 | −40,55 | −23,15 | 22,0 | 7,87 |
| 2026-01 → 2026-04 | 20 | +0,58 | −27,98 | −22,09 | 15,0 | 1,90 |
| 2026-04 → 2026-07 | 81 | −13,35 | −16,52 | −14,15 | 19,8 | 13,35 |
| 2026-07 → 2026-09 | 61 | −2,97 | +23,60 | +34,04 | 26,2 | 10,90 |
| **Composto** | **896** | **−21,75** | **−39,31** | **+85,84** | | **19,89** (pior janela) |

**Leitura:** expectativa **negativa** fora da amostra, de ≈ −0,16% por trade. O stop cheio (−7%) teve 72 saídas com média de −6,99%. As saídas por *trailing* (763) renderam só +0,60% em média, porque em 1h o ruído encerra os trades cedo. Os parâmetros escolhidos variam muito entre os treinos (`tp_trailing` de 0,6% a 4,9%, `min_score` de 0,26 a 0,88), sinal de que o otimizador ajustou ruído. O resultado ficou acima do *buy & hold* da cesta (−39,3%, puxado pelas *mid caps*), mas isso não compensa a expectativa negativa.

## 5. Conclusão

| Perfil | Critério de saída da Fase 3 | Situação |
|--------|-----------------------------|----------|
| conservador (4h) | ✅ Expectativa positiva após taxas fora da amostra (+0,46%/trade; +11,47% em 32 meses; pior DD de janela de 7,55%). Retorno bem abaixo do *buy & hold* do BTC (+85,8%), com risco muito menor | Apto a seguir para o *paper trading*, com parâmetros calibrados |
| moderado (1h) | ❌ Expectativa negativa (−0,16%/trade; −21,75%) | Não deve operar como está |

**Parâmetros robustos sugeridos para o conservador** (mediana dos 11 treinos, a validar no *paper trading*): `tp_activation` ≈ 10,8%, `tp_trailing` ≈ 130 bps, `atr_stop_mult` ≈ 2,8, `exit_score` ≈ −0,59, `min_score` ≈ 0,54, `adx_min` ≈ 21, `rsi_pullback_max` ≈ 38, `vol_rel_min` ≈ 2,4. A mediana usa treinos posteriores às primeiras janelas, então esses valores **não** foram validados fora da amostra como conjunto.

**Limitações conhecidas:**

- **Viés de sobrevivência:** a lista de pares é estática, formada por ativos que existem desde 2023. O universo real (D-013) muda com o tempo.
- **Execução idealizada:** preenchimento no preço do stop e de abertura do candle, sem *slippage* além da taxa. Em 4h, a ordem dos eventos dentro do candle é aproximada.
- **Sem a camada LLM** (Fase 4) e sem os limites do Risk Guard (Fase 5).
- **Amostra curta:** 11 janelas e 268 trades. O resultado depende de dois trimestres de alta forte.

