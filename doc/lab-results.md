# Lab — *walk-forward* results

> Result of the calibration of the profiles in use (`swing_trend` and `momentum_alpha`, 2026-10-01; revised on 2026-10-16, section 2). The full reports, window by window, are in `var/lab/` (not versioned).
> Past results do not guarantee future results. The numbers serve to **decide whether the agent should trade** and **are not investment advice**.

## 1. Method

| Item | Value |
|------|-------|
| Backtester | Freqtrade (`freqtradeorg/freqtrade:stable`) through Docker (D-014). The "shell" strategy imports `trade_agent.signals`, the same code as production |
| Intra-candle exits | Since 2026-10-16, `--detail 5m`: the stop and the trailing take-profit follow 5m candles. Without it, Freqtrade only sees the 4h high and low and assumes the high came first |
| Data | Public Binance Spot 4h candles, since 2021-10, for static pairs per *tier* (`lab/walk_forward.py`, `PAIRS_BY_TIER`): BTC and ETH; 10 *large*; 6 *mid*; 4 *small* |
| Windows | 15 quarterly validation windows, from 2023-01-01 to 2026-09-01 |
| Optimization (D-017) | For each window, *hyperopt* (`SharpeHyperOptLossDaily`, 150 epochs, seed 42) on the **previous 12 months**. The chosen parameters are applied **unchanged** to the window |
| Optimized parameters | `min_score`, `adx_min`, `rsi_pullback_max`, `vol_rel_min`, `exit_score`, `atr_stop_mult`, `tp_activation`, `tp_trailing` |
| Fixed (from the profile) | Maximum stop, risk per trade, maximum size, cash reserve, *break-even* and maximum holding. Since 2026-10-16 the lab simulates the agent's *break-even* (checked at the candle close, like the 4h cycle) and maximum holding |
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

## 2. Revision with intra-candle exits (2026-10-16)

**The optimized walk-forward of section 3 overstated the result.** With `--detail 5m`, the same method (Sharpe, 150 epochs, 12 months of training) gives **−4.95%** in `swing_trend` instead of +107.27%, and +24.91% in `momentum_alpha` instead of +34.36%. Without the intra-candle detail, *hyperopt* chose very tight trailings (0.6–1.4%) that only win because the 4h candle is read as "high first". `CalmarHyperOptLoss` gives +41.20% and +30.32%: still below the fixed parameters.

Method of the revision (D-034): **fixed parameters** (no *hyperopt* per window), with `--detail 5m`, *break-even* and maximum holding. Variants with **one change at a time** were compared on the **design windows (2023-01 → 2025-01, 8 windows)**; only the chosen one was run on the **holdout (2025-01 → 2026-09, 7 windows)**, without adjustment.

| Design (2023–2024), compounded | `swing_trend` | `momentum_alpha` |
|---|--:|--:|
| Profiles of 2026-10-01 (baseline) | +33.26% | +32.16% |
| Stop cap +1.5 p.p. | +43.31% | +39.69% |
| TP activation 13% | +36.98% | +44.89% |
| TP activation 7% | +23.44% | +27.99% |
| Trailing 200 / 300 bps | +17.05% / +18.80% | +27.15% / +29.48% |
| No *break-even* | +35.59% | +24.63% |
| `min_score` 0.55 / 0.78 | +19.48% / +31.14% | +33.45% / +36.54% |
| **Stop cap +1.5 p.p. and activation 13% (chosen)** | **+70.22%** | **+49.27%** |

| Holdout (2025-01 → 2026-09) | Baseline | Chosen variant |
|---|--:|--:|
| `swing_trend` | **+26.86%** (worst window DD 6.98%) | +9.47% (7.96%) |
| `momentum_alpha` | −0.02% (7.47%) | **+6.98%** (7.53%) |

**Adopted:** only in `momentum_alpha` (stop cap 6.5%, activation 13%): over the 15 windows, +32.14% → **+59.69%**, worst window drawdown 12.01% → **7.53%**. In `swing_trend` the gain did not hold on the holdout: in 2025–2026 the moves were smaller, and the trades that used to exit around +10% no longer reach the 13% activation. The fixed activation favored the strong trend of 2023–2024, and `swing_trend` stays as it was.

**Rejected:**

- **More risk per trade** (1.2 → 1.5% and 1.5 → 2.0%): better on the design, slightly worse on the holdout and with more drawdown.
- **New profiles:** `trend_daily` (1d, core and large; +34.47% on the design) loses in the same windows as the 4h profiles, so taking capital away from them lowers the portfolio's return (+63.5% → +55.2%) and raises its drawdown. `breakout_alt` (4h, mid and small) made +8.95%, and these tiers have the worst survivorship bias.

The holdout has been used, so any new variant tested on 2025–2026 no longer has a clean out-of-sample check: the Demo is the next validation.

### 2.1 ATR-based take-profit activation (2026-10-16)

Hypothesis: the fixed 13% activation won in 2023–2024 and lost in 2025–2026 because the moves were smaller, so an activation proportional to the entry's volatility (`take_profit.activation_atr_mult`: k × the ATR% of the signal candle, between `activation_pct` and `activation_max_pct`, D-035) could adapt to the regime. The median ATR% (4h) of the `swing_trend` entries was 2.41% in the design period and 1.99% in the holdout, so 9.7% is about 4–5 × ATR.

Same method (fixed parameters, `--detail 5m`, *break-even* and maximum holding), only the activation changes:

| `swing_trend` | Design (2023–2024) | Holdout (2025–2026) | 15 windows | Worst window DD | Trades |
|---|--:|--:|--:|--:|--:|
| Fixed 9.7% (current) | **+33.26%** | +26.86% | **+69.04%** | 12.57% | 265 |
| 4 × ATR, 7–16% | +25.80% | +23.71% | +55.63% | 12.69% | 268 |
| 5 × ATR, 7–16% | +15.31% | +21.88% | +40.54% | 12.65% | 256 |
| 6 × ATR, 7–16% | +23.91% | **+29.69%** | +60.69% | 12.65% | 252 |
| 5 × ATR, 9.7–13% | +24.66% | +18.74% | +48.02% | 12.57% | 256 |

**Rejected:** no variant beats the fixed activation on the design period, and the results are not monotonic in k (5 × ATR is worse than both 4 and 6), which points to noise rather than an effect. The ATR at the entry does not predict how far the trade will go: high-volatility entries get a higher activation, hold their slot longer and block new entries (2024-Q4: 50 trades and +26.72% with the fixed activation, 41 trades and +15.46% with 5 × ATR). The regime effect seen with the fixed 13% is not a per-trade volatility effect. The holdout was already used in this section, so the +29.69% of 6 × ATR is not an out-of-sample result either. The mode stays available (off by default) for future tests.

### 2.2 New profiles with ATR-based activation (2026-10-16)

Two new profiles were tried with the ATR-based activation, each against the same profile with a fixed activation (one change at a time). The 2025–2026 holdout had already been used in D-034 and section 2.1.

- `trend_atr`: 1d, core and large, 3 slots, risk 1.0%, stop 2 × ATR (cap 9%), trailing 250 bps, *break-even* 1.2R, maximum holding 40d, `min_score` 0.60, `vol_rel_min` 1.5.
- `alt_atr`: 4h, large and mid, 3 slots, risk 1.0%, stop 3 × ATR (cap 7%), trailing 150 bps, *break-even* 1.0R, maximum holding 10d, `min_score` 0.67.

| Profile | Activation | Design (2023–2024) | Holdout (2025–2026) | Worst window DD |
|---|---|--:|--:|--:|
| `trend_atr` | fixed 15% | +7.67% | −10.01% | 9.50% |
| `trend_atr` | 2.5 × ATR, 8–25% | +9.87% | −3.30% | 8.17% |
| `alt_atr` | fixed 11% | **+38.65%** | −6.25% | 9.34% |
| `alt_atr` | 4 × ATR, 8–18% | +27.26% | −1.40% | 8.14% |
| `alt_atr` | 4 × ATR, 10–13% | +23.29% | — | 8.74% |

**Rejected:** none of them is profitable on the holdout, so no profile was added. The ATR-based activation lost less than the fixed one on the holdout in both profiles, and in the daily profile it also won on the design period. On 4h it lost on the design period, as in `swing_trend`. Both profiles lose in 2025–2026, the regime in which `swing_trend` still made +26.86%. An earlier daily test (`trend_daily`, D-034) had made +34.47% on the design period with other entry filters.

## 3. Out-of-sample results (2026-10-01)

> **Superseded by section 2:** these numbers come from 4h backtests without intra-candle exits and are inflated by the tight trailings that *hyperopt* chose.

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

## 4. Adopted parameters

`config/profiles.yaml` uses the **median of the 15 trainings** of each profile (D-028):

| Parameter | `swing_trend` | `momentum_alpha` |
|-----------|--------------:|-----------------:|
| `min_score` | 0.67 | 0.67 |
| `adx_min` | 24 | 29 |
| `rsi_pullback_max` | 41 | 35 |
| `vol_rel_min` | 2.3 | 2.6 |
| `exit_score` | −0.46 | −0.46 |
| `atr_mult` (stop) | 3.1 | 3.0 |
| `max_pct` (stop) | 4.5 | **6.5** (D-034; was 5.0) |
| `activation_pct` (TP) | 9.7 | **13.0** (D-034; was 9.5) |
| `trailing_delta_bps` (TP) | 100 | 130 |

The median uses trainings later than the first windows, so this set was **not** validated out of sample as a whole. The Demo (doc 04) is the validation that counts.

## 5. Limitations

- **Survivorship bias:** the pair list is static, made of assets that exist since 2021. The real universe changes every cycle (D-027), and most of the recent setups showed up in assets outside that list.
- **Idealized execution:** fills at the stop price and at the candle open, with no *slippage* beyond the fee. With `--detail 5m` the order of events within the 4h candle follows 5m candles; within each 5m candle it is still approximated.
- **No LLM layer** and no Risk Guard limits.
- **Short sample:** 15 windows and about 300 trades per profile.

## 6. History

The first tests (2026-09-26) used the `conservador` (4h) and `moderado` (1h) profiles, since removed. The baseline with fixed parameters had a negative expectancy in both profiles. Losses went to the full stop while the trailing took small gains, and there were *altcoin* buys with BTC falling and breakouts against the trend. That led to the regime filter and the with-the-trend-only setups (D-016) and to the optimized *walk-forward* (D-017). On 1h, the excess of trades and the fees made the expectancy negative even with optimization, and the current profiles use 4h.
