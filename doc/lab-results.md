# Lab — *walk-forward* results

> Result of the calibration of the profiles in use (`swing_trend` and `momentum_alpha`, 2026-10-01). The full reports, window by window, are in `var/lab/` (not versioned).
> Past results do not guarantee future results. The numbers serve to **decide whether the agent should trade** and **are not investment advice**.

## 1. Method

| Item | Value |
|------|-------|
| Backtester | Freqtrade (`freqtradeorg/freqtrade:stable`) through Docker (D-014). The "shell" strategy imports `trade_agent.signals`, the same code as production |
| Data | Public Binance Spot 4h candles, since 2021-10, for static pairs per *tier* (`lab/walk_forward.py`, `PAIRS_BY_TIER`): BTC and ETH; 10 *large*; 6 *mid*; 4 *small* |
| Windows | 15 quarterly validation windows, from 2023-01-01 to 2026-09-01 |
| Optimization (D-017) | For each window, *hyperopt* (`SharpeHyperOptLossDaily`, 150 epochs, seed 42) on the **previous 12 months**. The chosen parameters are applied **unchanged** to the window |
| Optimized parameters | `min_score`, `adx_min`, `rsi_pullback_max`, `vol_rel_min`, `exit_score`, `atr_stop_mult`, `tp_activation`, `tp_trailing` |
| Fixed (from the profile) | Maximum stop, risk per trade, maximum size and cash reserve. *Break-even* and maximum holding are not simulated |
| Simulated protection | Fixed stop `entry × (1 − min(k·ATR%, max_stop))` and a *trailing take-profit* with activation, like the native OPOCO |
| Fees | 0.1% per side (standard Spot fee, without the BNB discount) |
| References | *Buy & hold* of the profile's pair basket (simple average) and of BTC |

```bash
uv run python -m lab.walk_forward --profile swing_trend --optimize --download
uv run python -m lab.walk_forward --profile momentum_alpha --optimize --download
uv run python -m lab.walk_forward --profile swing_trend          # the profile's current parameters, no optimization
```

`--download` fetches the candles from 15 months before the first window (3 of warm-up and 12 of training). Freqtrade's `download-data` does not extend a pair that already has data backwards. For that, run it once with `--prepend`:

```bash
docker compose -f lab/freqtrade/docker-compose.yml run --rm freqtrade download-data \
    --config user_data/config.swing_trend.json --timerange 20211001- -t 4h --prepend
```

## 2. Out-of-sample results (2026-10-01)

| | `swing_trend` | `momentum_alpha` |
|---|---:|---:|
| Windows with a positive result | 7/15 | 10/15 |
| Windows above the basket *buy & hold* | 8/15 | 8/15 |
| Windows above the BTC *buy & hold* | 8/15 | 6/15 |
| **Compounded result** | **+107.27%** | **+34.36%** |
| Compounded basket *buy & hold* | +170.96% | +60.80% |
| Compounded BTC *buy & hold* | +375.05% | +375.05% |
| Trades | 316 | 301 |
| Worst window drawdown | 11.20% | 13.68% |

**Reading:**

- **Positive expectancy after fees, out of sample**, in both profiles.
- **Return well below *buy & hold***, with much lower risk. In the negative windows, the losses were down to −5% in `swing_trend` and down to −13% in `momentum_alpha` (2023-Q3). In those windows, the basket fell as much as −37% and −40%.
- **Concentration:** in `swing_trend`, the result comes mostly from three bullish quarters (2023-Q4 +19.9%, 2024-Q1 +14.1% and 2024-Q4 +49.3%). `momentum_alpha` spreads the gains more (10 positive windows), but with a lower total return.
- **Parameter stability:** the take-profit activation stayed between 6% and 11.5% in the trainings of both profiles, a consistent sign of letting profits run. The entry parameters vary a lot between trainings (`min_score` from 0.31 to 0.88; `adx_min` from 10 to 34), which points to little predictive power of the entry filters on their own.

## 3. Adopted parameters

`config/profiles.yaml` uses the **median of the 15 trainings** of each profile (D-028):

| Parameter | `swing_trend` | `momentum_alpha` |
|-----------|--------------:|-----------------:|
| `min_score` | 0.67 | 0.67 |
| `adx_min` | 24 | 29 |
| `rsi_pullback_max` | 41 | 35 |
| `vol_rel_min` | 2.3 | 2.6 |
| `exit_score` | −0.46 | −0.46 |
| `atr_mult` (stop) | 3.1 | 3.0 |
| `activation_pct` (TP) | 9.7 | 9.5 |
| `trailing_delta_bps` (TP) | 100 | 130 |

The median uses trainings later than the first windows, so this set was **not** validated out of sample as a whole. The Demo (doc 04) is the validation that counts.

## 4. Limitations

- **Survivorship bias:** the pair list is static, made of assets that exist since 2021. The real universe changes every cycle (D-027), and most of the recent setups showed up in assets outside that list.
- **Idealized execution:** fills at the stop price and at the candle open, with no *slippage* beyond the fee. On 4h, the order of events within the candle is approximated.
- **No LLM layer**, no Risk Guard limits, no *break-even* and no maximum holding.
- **Short sample:** 15 windows and about 300 trades per profile.

## 5. History

The first tests (2026-09-26) used the `conservador` (4h) and `moderado` (1h) profiles, since removed. The baseline with fixed parameters had a negative expectancy in both profiles. Losses went to the full stop while the trailing took small gains, and there were *altcoin* buys with BTC falling and breakouts against the trend. That led to the regime filter and the with-the-trend-only setups (D-016) and to the optimized *walk-forward* (D-017). On 1h, the excess of trades and the fees made the expectancy negative even with optimization, and the current profiles use 4h.
