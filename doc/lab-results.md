# Lab — *walk-forward* results

> Result of the calibration of the profiles in use (`swing_trend` and `momentum_alpha`, 2026-10-01; revised on 2026-10-16, section 2; `alt_atr` created on 2026-10-17, section 2.3; stop-noise filter on 2026-10-22, section 2.4; re-run on 999 candles on 2026-10-22, sections 2.5 and 2.6; stop-noise filter re-checked on 999 candles on 2026-10-23, section 2.7; sections 2 to 2.6 re-run on the corrected lab on 2026-10-25, section 2.8). The full reports, window by window, are in `var/lab/` (not versioned).
> Past results do not guarantee future results. The numbers serve to **decide whether the agent should trade** and **are not investment advice**.

## 1. Method

| Item | Value |
|------|-------|
| Backtester | Freqtrade (`freqtradeorg/freqtrade:stable`) through Docker (D-014). The "shell" strategy imports `trade_agent.signals`, the same code as production |
| Intra-candle exits | Since 2026-10-16, `--detail 5m`: the stop and the trailing take-profit follow 5m candles. Without it, Freqtrade only sees the 4h high and low and assumes the high came first |
| Data | Public Binance Spot 4h candles, since 2021-07 (core, large and mid; 4h and 5m prepended on 2026-10-22), for static pairs per *tier* (`lab/walk_forward.py`, `PAIRS_BY_TIER`): BTC and ETH; 10 *large*; 6 *mid*; 4 *small* |
| Universe (`--alts`) | Since 2026-10-22, the tier lists plus 18 volatile alts (`ALTS`), each one tradable only while its 24h quote volume reaches the universe's minimum (5 M USDT), as in the agent (section 2.4) |
| Windows | 15 quarterly validation windows, from 2023-01-01 to 2026-09-01 |
| Indicator history | Since 2026-10-22, Freqtrade loads 999 candles before each window (`startup_candle_count` = `FeatureParams.history`), the same history as the agent (D-040, section 2.5). The earlier runs loaded 201, so the EMA 200 started each window at its SMA seed |
| Optimization (D-017) | For each window, *hyperopt* (`SharpeHyperOptLossDaily`, 150 epochs, seed 42) on the **previous 12 months**. The chosen parameters are applied **unchanged** to the window |
| Optimized parameters | `min_score`, `adx_min`, `rsi_pullback_max`, `vol_rel_min`, `exit_score`, `atr_stop_mult`, `tp_activation`, `tp_trailing` |
| Fixed (from the profile) | Maximum stop, risk per trade, maximum size, cash reserve, *break-even* and maximum holding. Since 2026-10-16 the lab simulates the agent's *break-even* (checked at the candle close, like the 4h cycle) and maximum holding |
| Simulated protection | Fixed stop `entry × (1 − min(k·ATR%, max_stop))` and a *trailing take-profit* with activation, like the native OPOCO |
| Risk Guard R:R | Since 2026-10-17, no entry when `(activation − fees) ÷ (stop + fees)` is below `pre_trade.min_reward_risk` (`config/stop_conditions.yaml`), like the agent. The 2026-10-16 profiles always pass it |
| Fees | 0.1% per side (standard Spot fee, without the BNB discount) |
| References | *Buy & hold* of the profile's pair basket (simple average) and of BTC |

```bash
uv run python -m lab.walk_forward --profile swing_trend --optimize --download
uv run python -m lab.walk_forward --profile momentum_alpha --optimize --download
uv run python -m lab.walk_forward --profile swing_trend          # the profile's current parameters, no optimization
uv run python -m lab.walk_forward --profile swing_trend --alts   # with the alts the universe takes in during a pump
```

`--download` fetches the candles from 18 months before the first window on 4h: 6 months of warm-up for the 999 startup candles and 12 of training. When the data starts later, Freqtrade moves the start of the window forward (with the data since 2021-10, as before 2026-10-22, the first training started in 2022-03 instead of 2022-01). Freqtrade's `download-data` does not extend a pair that already has data backwards. For that, run it once with `--prepend` (the `momentum_alpha` configuration has the core, large and mid pairs):

```bash
docker compose -f lab/freqtrade/docker-compose.yml run --rm freqtrade download-data \
    --config user_data/config.momentum_alpha.json --timerange 20210701- -t 4h 5m --prepend
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

### 2.3 `alt_atr`: experimental profile at 5% (2026-10-17)

Goal: a profile with a small share (5%, 50 USDT) and some profit, or the smallest loss, in 2025–2026, first with the ATR-based activation. Starting from the `alt_atr` of section 2.2, 44 walk-forwards over the 15 windows (2023-01 → 2026-09) with one or two changes at a time, all with the 50 USDT wallet. The 2025–2026 holdout had been used before and was used again here to choose, so these numbers are **not** out of sample.

**Sweep without the R:R check** (base: 4 × ATR, 8–18%, relative volume ≥ 2.3; holdout −1.70%, 15 windows +25.12%). Relative volume was the only lever that improved both periods: ≥ 1.8 gave +57.95% / +3.81% (+63.97%), and 1.6, 2.0 and 2.8 were also positive on the holdout. Two slots, `min_score`, ADX, *break-even*, maximum holding, trailing, stop cap, tiers and exit score did not improve both periods. With 5% or 30% of the capital the result is the same (no minimum notional effect).

**The Risk Guard refuses entries with R:R below 1.5** (`pre_trade` in `config/stop_conditions.yaml`), and the lab did not simulate it: 4 × ATR against a 3 × ATR stop is R:R 1.33, so the agent would refuse most of those entries. The lab now applies the same check, and a test checks that every repository profile passes it for any ATR:

| `alt_atr`, relative volume ≥ 1.8, with the R:R check | Design (2023–2024) | Holdout (2025–2026) | 15 windows | Worst window | Worst window DD | Trades |
|---|--:|--:|--:|--:|--:|--:|
| 4 × ATR, 8–18% (stop 3 × ATR) | +32.50% | −16.24% | +10.99% | −12.12% | 12.12% | 364 |
| 4 × ATR, stop 2.5 / 2 × ATR | +37.68% / +24.80% | −12.98% / −17.04% | +19.81% / +3.53% | | 10.71% / 11.64% | |
| 5 × ATR, 8–20% | +50.35% | +0.43% | +50.99% | −5.43% | 10.09% | 426 |
| 5 × ATR, 8–16% | +47.37% | +1.05% | +48.93% | −5.43% | 10.09% | 435 |
| 5 × ATR with relative volume 1.6 / 2.0 | +61.61% / +33.84% | −6.50% / +0.01% | +51.10% / +33.85% | | 11.69% / 8.79% | |
| 5 × ATR with floor 10%, trailing 100, `min_score` 0.72 or 2 slots | +40% to +57% | −7.01% to −0.80% | +30% to +56% | | 9.3–10.9% | |
| 5.5 / 6 × ATR | +37.00% / +40.79% | −3.24% / −1.52% | +32.55% / +38.65% | | 10.9% / 10.5% | |
| Fixed 11%, float R:R (see below) | +20.47% | +11.15% | +33.90% | −3.03% | 6.62% | 192 |
| Fixed 11%, R:R as in the agent | +42.50% | −5.17% | +35.14% | −6.65% | 10.85% | 458 |
| **Fixed 10.9% (adopted)** | **+20.26%** | **+8.69%** | **+30.71%** | **−3.93%** | **6.62%** | 190 |

**Reading:** around 5 × ATR the holdout stays between −7% and +1%: in 2025–2026 the ATR-based profile is about break-even, and its result comes from the strong 2023–2024 trend in the alts.

**Adopted: fixed 10.9% activation**, the smallest loss in 2025–2026. The gain does not come from the activation level itself but from the R:R check: with the stop at its 7% cap (ATR above about 2.3%), (10.9 − 0.2) ÷ (7 + 0.2) = 1.49 is below 1.5 and the Risk Guard refuses the entry. The profile only trades the less volatile setups, about 190 trades instead of 460, with a third of the drawdown. At 11% the ratio at the cap is exactly 1.5: the agent (Decimals) accepts those entries, while the first lab run (floats, 1.4999…) refused them. With the agent's arithmetic, 11% gives −5.17% in 2025–2026; the lab now uses the same tolerance, and a test pins the 10.9% behavior. With 5% of the capital, the worst window (−3.93%) is about 2 USDT, and the portfolio of the three profiles goes from +67.28% to +65.75% over the 15 windows (holdout +19.62% → +19.72%; worst window −3.71% → −3.62%). The Demo decides whether the share grows.

### 2.4 Real Demo trades and the stop inside the noise (2026-10-22)

**Real sample** (Demo, 2026-10-01 → 2026-10-22, 45 positions read from PostgreSQL; setups rebuilt with the agent's code and its 251-candle window, excursions measured on public 5m candles). The agent made 12 gains and 32 losses, for −70.62 USDT. There were 33 stops (−121.54, three of them at *break-even*) and 11 trailing take-profits (+50.92). With an average gain of +7.1% and an average loss of −4.5%, the profile needs a 39% hit rate; the agent had 27%. 43 entries were breakouts; the 2 pullbacks were in BTC. Three weeks and a single regime make this a small sample.

The entries bought pumps. At the signal candle (medians), the close was 29% above the EMA 50 (4.3 ATR), with RSI 79.5, +26% in 24h, a candle body of 1.8 ATR and 24h volume at 10× its 30-day median. Most of these assets (MOVR, GTC, RLC, MAGIC, OGN, ...) normally trade below the universe's 5 M USDT a day. They join it only during the pump, often in the *large* tier (D-027), and the lab's static lists never had them. With a median ATR of 5.3%, the 4.5–5% stop caps left a median stop of **0.9 ATR**. The median time to the stop was 2 h, and 18 of the 30 stops below −2% traded back above the entry within 72 h. Winners were as stretched as losers (RSI 82 vs 79), so the entry stretch alone does not separate them.

**Method:** the same as section 2 (fixed parameters, `--detail 5m`, *break-even*, maximum holding and the R:R check), with one change at a time, chosen on the design windows and only then run on the holdout. Each variant ran on the tier lists and with `--alts`.

| Design (2023–2024), compounded | `swing_trend` tier / alts | `momentum_alpha` tier / alts | `alt_atr` tier / alts |
|---|--:|--:|--:|
| Baseline | +33.25% / **−21.20%** (DD 21.3%) | +49.29% / **−23.11%** (DD 26.4%) | +20.26% / +20.15% |
| Close ≤ 3 ATR above the EMA 50 | +11.13% / −11.30% | +11.14% / −30.39% | +4.02% / +2.61% |
| Pullback only (RSI ≤ 50, no breakout) | −10.50% / −21.26% | −5.27% / −12.08% | +2.36% / +1.29% |
| Limit entry 0.5 ATR below the close, for one candle | +31.60% / +15.59% (DD 16.4%) | +24.11% / +18.31% | +17.96% / +22.42% |
| Exit at RSI ≥ 80 in profit | +26.64% / −31.46% | +31.20% / −19.97% | +8.96% / +9.34% |
| Universe by the 30-day median volume | — / +6.55% (DD 18.6%) | — / +16.98% (DD 22.1%) | — / +21.32% |
| **Stop ≥ 1.5 ATR after the cap** | +29.87% / **+39.54%** (DD 9.8%) | +49.69% / −6.53% (DD 16.3%) | same as the baseline |

With `--alts`, the baseline reproduces the Demo. In `swing_trend`, the 266 alt trades lost 357 USDT (26% hit rate), while the core and *large* trades made 221. Only the stop filter kept every window's drawdown under 13% with the alts. In `alt_atr` the filter changes nothing, because its R:R check (D-036) already refuses the entries whose stop is at its cap.

Sensitivity to the multiple k (design, tier / alts):

- `swing_trend`: k = 1.25 → +40.95% / +27.25% (DD 14.3%); 1.5 → +29.87% / +39.54%; 2 → +30.21% / +26.58%. The gain holds from 1.25 to 2, so the value fixed before the run, 1.5, stays.
- `momentum_alpha`: k = 1.5 → +49.69% / −6.53%; 2 → +32.34% / +28.26% (DD 9.8%); 3 → +18.58% / +20.34%. k = 2 is the smallest value within the drawdown cap.

| Holdout (2025-01 → 2026-09) | Tier | Alts | Alts + 11 newer ones (ENA, STRK, PYTH, W, ...) | 15 windows, tier / alts |
|---|--:|--:|--:|--:|
| `swing_trend` baseline | +26.88% | +46.07% | +40.52% | +69.07% / +15.11% (DD 21.3%) |
| `swing_trend`, k = 1.5 | **+32.29%** | +29.90% | +27.29% | **+71.81% / +81.27%** (DD 9.8%) |
| `momentum_alpha` baseline | +7.02% | −1.61% | +2.14% | +59.77% / −24.34% (DD 26.4%) |
| `momentum_alpha`, k = 2 | **+11.69%** | **+11.03%** | **+6.53%** | +47.82% / **+42.41%** (DD 9.8%) |
| `alt_atr` (unchanged) | — | +5.59% | +5.60% | — / +26.87% |

**Adopted (D-039):** `signals.min_stop_atr` 1.5 in `swing_trend`, which skips setups with an ATR above 3%, and 2.0 in `momentum_alpha` (ATR above 3.25%). Weighted by the capital shares (65/30/5%), the 15 windows go from +65.2% to +62.2% with the tier lists (worst window −3.6% → −6.4%). With the alts, they go from **+3.2% to +66.4%** (worst window **−14.9% → −4.2%**).

The cost is the alt seasons: in `swing_trend` the alts made +31% in 2025-Q3, and the holdout with alts falls from +46.07% to +29.90%. On the 44 real trades, the filter would have skipped 38 (−57.50 USDT) and kept 6 (−13.12 USDT). The holdout had already been used (D-034 to D-036), so this is **not** a clean out-of-sample result; the Demo is the next validation.

**Rejected:**

- The stretch filter and the pullback-only setup lose on both baskets: the breakouts that work are as stretched as the ones that fail.
- The limit entry below the close and the RSI exit lose on the tier lists. With the alts in `swing_trend`, the limit entry also goes over the drawdown cap.
- The 30-day median volume removes the illiquid alts, but not the pumps of the liquid ones (drawdown 18–22%).

**Also found:**

1. With its 251-candle EMA 200 (section 5), the agent bought COMPUSDT on 2026-10-20 on a breakout that the full history does not show.
2. The trailing `TAKE_PROFIT` fills at market. 3 of the 11 take-profits triggered on spikes seen only on the Demo (the public high stayed below the activation) and filled 4–12% below the trigger.

### 2.5 Indicator history: the agent and the lab on 999 candles (2026-10-22)

TA-Lib starts the EMA 200 from the SMA of its first 200 candles, and the seed fades by a factor of 1 − 2/201 per candle. The agent computed the features on the last 251 closed candles (`warmup + 50`), where the seed still weighs ≈ 60% of the EMA 200. The lab was not on the full history either: Freqtrade loads only `startup_candle_count` candles (201) before each window, so every quarterly window started with the EMA 200 at its seed, and it converged only weeks later. The trend check (EMA 50 above EMA 200, close above EMA 200) and BTC's regime depended on how many candles each side had. On 2026-10-20 the agent bought COMPUSDT (`momentum_alpha`) on a breakout with score 0.85. With the full history the EMA 200 was 22.676 instead of 22.506, the score was 0.35 and there was no setup.

**Measurement:** each candle from 2025-01-01 to 2026-09-01 of the profile's pairs, with the profile's parameters. The agent's side takes the last N candles of the asset and of BTC, as `DecisionEngine` does. The lab's side loads the candles the way Freqtrade does, quarterly windows with S candles before each one. Both are compared with the features of the whole history since 2021-10. The table counts the entry signals (setup and `min_score`) found by only one of the two sides, as a share of all the signals found by either side.

| Entry signals that differ from the whole history | `swing_trend` (12 pairs, 185 signals) | `momentum_alpha` (18 pairs, 174 signals) |
|---|--:|--:|
| Agent, 251 candles (before) | 39 (19.1%) | 32 (16.8%) |
| Agent, 500 / 750 candles | 5 (2.7%) / 0 | — |
| Agent, 999 candles (now) | **0** | **0** |
| Lab, startup 201 (before) | 17 (9.0%) | 15 (8.4%) |
| Lab, startup 999 (now) | **0** | **0** |
| Lab against the agent, before (201 / 251) | 34 (17.3%) | 29 (15.6%) |
| Lab against the agent, now (999 / 999) | **0** | **0** |

**Adopted (D-040):** `FeatureParams.history` = 999 closed candles. That is one klines request: 1000 candles, the last one still open, with weight 2 like the old 252. The agent fetches them for every asset and for BTC, and the lab uses the same number as `startup_candle_count`. After 999 candles the seed weighs ≈ 0.03%, and on `swing_trend` 750 candles already removed every difference in the sample. On the Demo, a gzip response grows from about 13 KB to 50 KB and takes up to 0.3 s more on a reused connection: at most about 20 s more per close for the ~60 assets of the universe. The closed-candle cache holds up to 256 frames of about 72 KB each (≈ 18 MB at most).

The earlier calibrations (sections 2 to 4) ran with the 201-candle startup. Section 2.6 runs the fixed-parameter ones again with 999.

### 2.6 The fixed-parameter calibrations on 999 candles (2026-10-22)

Same method as sections 2 and 2.3 (fixed parameters, `--detail 5m`, *break-even*, maximum holding and the R:R check, 15 windows from 2023-01 to 2026-09). Each profile ran on the same code and data with `startup_candle_count` 201 and 999, so the difference is the indicator history alone. The 4h and 5m data of the core, large and mid pairs now start on 2021-07-01 (`--prepend --timerange 20210701-`). That changes nothing in these windows, because the 999 candles before 2023-01-01 start in 2022-07, but it gives the optimized runs their full first training. With 201, the runs reproduce the published numbers: to the cent for `swing_trend` and `alt_atr`, and within 0.12 p.p. for `momentum_alpha` (+59.81% instead of +59.69% over the 15 windows). The portfolio makes +65.79% instead of +65.75%.

| 201 → 999 candles | Design (2023–2024) | Holdout (2025–2026) | 15 windows | Worst window | Worst window DD | Trades |
|---|--:|--:|--:|--:|--:|--:|
| `swing_trend` | +33.26% → **+41.85%** | +26.86% → **+28.14%** | +69.04% → **+81.76%** | −5.29% → −4.56% | 12.57% → 12.57% | 265 → 264 |
| `momentum_alpha` | +49.31% → +40.93% | +7.03% → **+14.13%** | +59.81% → +60.84% | −5.18% → −4.72% | 7.52% → 9.81% | 245 → 243 |
| `momentum_alpha`, startup trimmed by date (see below) | +49.31% → +49.19% | +7.03% → **+14.13%** | +59.81% → **+70.27%** | −5.18% → −4.72% | 7.52% → 7.52% | 245 → 245 |
| `alt_atr` | +20.26% → +19.00% | +8.69% → +5.38% | +30.71% → +25.40% | −3.93% → −4.33% | 6.62% → 6.34% | 190 → 180 |
| Portfolio (65 / 30 / 5%) | +38.46% → +41.53% | +19.74% → +22.65% | +65.79% → **+73.59%** | −3.62% → −3.67% | | |

**Where it changed:** `swing_trend` gains most in 2023-Q4 (+7.66% → +10.99%) and 2024-Q2 (−2.67% → +0.28%), with 11 positive windows instead of 9. `momentum_alpha` gains in 2023-Q4 (+17.05% → +21.63%) and in 2025–2026 (2025-Q1 −2.69% → −0.42%, 2025-Q3 +11.10% → +15.22%, 2026-Q1 −5.18% → −3.80%), but loses in 2024-Q4 (+16.18% → +10.80%). `alt_atr` loses in 2025-Q2 (−1.22% → −2.62%) and 2026-Q1 (−2.39% → −4.33%), with 10 fewer trades.

**The choices of D-034 and D-036 still hold** (published numbers on 201 candles in the last column):

| At 999 candles | Design (2023–2024) | Holdout (2025–2026) | Holdout on 201 |
|---|--:|--:|--:|
| `swing_trend` as it is vs stop cap 6% and activation 13% (rejected) | +41.85% vs +68.77% | **+28.14%** vs +10.04% | +26.86% vs +9.47% |
| `momentum_alpha` stop cap 6.5% and activation 13% (adopted) vs 5% and 9.5% | **+40.93%** vs +22.37% | **+14.13%** vs +3.91% | +6.98% vs −0.02% |
| `alt_atr` fixed 10.9% (adopted) vs 5 × ATR 8–20% / fixed 11% | +19.00% vs +38.64% / +36.29% | **+5.38%** vs −4.85% / −6.82% | +8.69% vs +0.43% / −5.17% |

With the startup trimmed by date, `momentum_alpha` against its 2026-10-01 parameters is +49.19% vs +29.29% on the design period. The 2025–2026 holdout does not change.

**APT and the startup count:** Freqtrade cuts `startup_candle_count` candles from the start of each pair's data, not the candles before the window's date. A pair with less history before a window therefore loses the start of the window. APT (listed on 2022-10-19, 444 candles before 2023-01-01) is out of 2023-Q1 and out of the first 2.5 days of 2023-Q2, while the agent evaluates any asset that has 201 candles. With 201, APT gave `momentum_alpha` two trailing exits in 2023-Q1 (+17.67% and +11.61%, 18 of the window's 44 USDT). With the startup trimmed by date (an experiment outside the lab code at the time, the lab's default since section 2.7; with section 2.7's corrected entry stop, the "trimmed by date" row is +46.53% / +14.91% / +68.38%), `momentum_alpha` makes +17.22% in 2023-Q1 instead of +10.73%, and the other 14 windows are identical. `alt_atr` did not trade APT there and does not change. In the tier lists only APT is affected, but in a universe with recent listings every new asset would be missing from the lab for its first 999 candles (about 5.5 months on 4h).

**Not run again:** the ATR-based activation (sections 2.1 and 2.2), the optimized walk-forward (section 3, superseded) and the medians of section 4. The 2025–2026 holdout had already been used, so these numbers are not out of sample either.

**Result:** no calibration changes and `config/profiles.yaml` stays as it is. With the EMA 200 off its seed, `swing_trend` and `momentum_alpha` do better in 2025–2026, and `alt_atr` does slightly worse (+5.38% instead of +8.69%) but is still the only one of its variants with a profit there.

### 2.7 The stop-noise filter on 999 candles (2026-10-23)

Sections 2.4 (D-039, measured on the 201-candle startup) and 2.6 (D-040, without `min_stop_atr` or `--alts`) had never run together. This section re-runs D-039's `k` on the combined code: 999 candles, `--detail 5m`, *break-even*, maximum holding and the R:R check, 15 windows from 2023-01 to 2026-09, on both the tier lists and `--alts`.

**The startup is now trimmed by date.** Freqtrade drops `startup_candle_count` rows from the start of each pair's data (section 2.6). The 18 `--alts` pairs only have candles since 2022-10-01, about 540 4h candles before 2023, so with 999 they would have been out of 2023-Q1 and part of 2023-Q2, which are the design windows. The lab strategy now passes `startup_candles=0` to both of Freqtrade's backtesting trims, so they cut at the window's start date. That is identical for pairs with the full history, and a recent listing trades from the window's start, as in the agent, which evaluates any asset with 201 candles. Check: `momentum_alpha` in 2023-Q1 makes +17.22% (24 trades), the "trimmed by date" number of section 2.6, and both k = 0 baselines reproduce section 2.6 exactly (`swing_trend` +81.76% and `momentum_alpha` trimmed by date +70.27% over the 15 windows).

**Causality and the entry's stop (review of PR #34).** Two corrections to the lab strategy; every number in this section comes from the corrected one.

1. *Look-ahead with the trim by date.* Freqtrade caches the analyzed dataframe before the trim and points the callbacks' cursor at `startup_candle_count + row`. A pair with less history before the window loses fewer rows, so the cursor ran ahead of time: for the `--alts` pairs (552 candles before 2023), the break-even check and the position size read candles up to ~74 days ahead. The callbacks now read only the candles closed at `current_time` (`_closed_candles`), whatever the cursor.
2. *The entry's stop came from the wrong candle.* `_entry_value` looked up `timeframe_to_prev_date(open_date)`, which is the entry's own 4h candle, still open at the entry. With the cursor ahead, the stop used that candle's final high and low. With the full history (any pair, since the lab's start, D-014), that candle was not visible yet: the stop sat at the cap for the first 4 h, then switched to the entry candle's own stop. The agent sizes the position and places the OCO with the signal candle's stop, fixed from then on, and the lab now does the same: stop, ATR activation and size all come from the last candle closed at the entry.

`tests/integration/lab/test_lab_causality.py` checks both. One test runs the pinned image's backtest twice, rewriting the alts' candles after 2023-02-01, and every earlier entry and exit must stay the same. The other runs the review's callback case on the image's real `DataProvider` with the cursor ahead: changing the unfinished entry candle's high and low (100.4/99.6 → 110/90) must not change the stop at 04:05 nor the size. On the reviewed strategy that stop moved from 2.63% to the 4.5% cap. Against the first run of this section, the corrections move the rows by up to ~4 p.p., and the same `k` values win. **Sections 2 to 2.6** (and the calibrations they support: D-034, D-035, D-036 and the original D-039 runs) used the previous entry stop; section 2.8 re-runs them.

**Rule, fixed before the runs:** choose on the design windows (2023–2024), on both baskets, with the worst window drawdown at most 13% on both. Keep the current `k` unless another value beats it on the design of **both** baskets. The 2025–2026 holdout only confirms.

| Design 2023–2024 (tier / `--alts`) | Compounded | Worst window DD | Holdout 2025–2026 (tier / `--alts`) |
|---|--:|--:|--:|
| `swing_trend` k = 0 | +42.49% / −12.11% | 12.52% / **21.31%** | +25.78% / +36.79% |
| `swing_trend` k = 1.0 | +57.05% / +36.23% | 11.61% / **17.74%** | +25.78% / +19.55% |
| **`swing_trend` k = 1.5 (current)** | +35.25% / **+42.13%** | 9.78% / 9.72% | +27.37% / +19.07% |
| `swing_trend` k = 2.0 | +38.36% / +34.48% | 4.99% / 6.12% | +18.56% / +18.56% |
| `momentum_alpha` k = 0 | +46.53% / −10.92% | 7.41% / **26.42%** | +14.91% / −6.92% |
| `momentum_alpha` k = 1.5 | +46.99% / +3.16% | 9.81% / **16.31%** | +17.88% / +6.00% |
| **`momentum_alpha` k = 2.0 (current)** | +29.95% / **+31.10%** | 7.46% / 9.79% | +18.37% / +7.73% |
| `momentum_alpha` k = 2.5 | +19.30% / +22.50% | 6.99% / 5.79% | +17.19% / +11.91% |
| **`alt_atr` without the filter (current)** | +18.46% / +18.95% | 6.19% / 6.16% | +4.05% / +1.36% |
| `alt_atr` k = 1.5 | identical | identical | identical |

| Portfolio 65 / 30 / 5% (15 windows) | Without the filter | With the current `k` |
|---|--:|--:|
| Tier lists | +74.49% (worst window −3.40%) | +64.49% (−6.22%) |
| `--alts` | +9.10% (worst window **−14.15%**) | **+58.19%** (−5.79%) |

**Result: no calibration change.** k = 1.0 (`swing_trend`) and k = 1.5 (`momentum_alpha`) break the 13% drawdown cap on `--alts`. `swing_trend` k = 2.0 beats k = 1.5 on the tier lists but not on `--alts`, and `momentum_alpha` k = 2.5 is worse on both. `alt_atr` does not change with the filter, because its R:R check (D-036) already refuses those entries. The numbers agree with section 2.4 (portfolio with `--alts`: +3.2% → +66.4% there, +9.1% → +58.2% here). The cost is the same too: the filter gives up the alt seasons (portfolio holdout on `--alts` +21.05% → +14.69%). The 2025–2026 holdout had already been used, so the Demo remains the validation.

### 2.8 Sections 2 to 2.6 on the corrected lab (2026-10-25)

The decisions of sections 2 to 2.6 were re-run on the lab of section 2.7: 999 candles, the startup trimmed by date, causal callbacks and the entry stop from the signal candle. Each variant is the current profile of `config/profiles.yaml` with the same single change as the original section, on the tier lists, over 15 windows (40 walk-forwards). The current profiles already carry D-039's filter, so each check answers whether the rejected or adopted alternative still loses or wins today. The baselines are section 2.7's current profiles: `swing_trend` +35.25% design / +27.37% holdout (worst window DD 9.78%), `momentum_alpha` +29.95% / +18.37% (7.46%), `alt_atr` +18.46% / +4.05% (6.19%), and the 65/30/5 portfolio +33.24% / +23.45%, +64.49% over the 15 windows.

| Decision | Variant (design / holdout, compounded) | Then | Now |
|---|---|---|---|
| D-034 `momentum_alpha` cap 6.5%, activation 13% (adopted) | before D-034, 5% / 9.5%: +15.59% / +13.78%; 5% / 13%: +15.86% / +16.84%; 6.5% / 9.5%: +17.44% / +15.09% | adopted | **holds** |
| D-034 `swing_trend` cap 6%, activation 13% (rejected on the holdout) | +72.40% / +14.24% | rejected | **holds** |
| D-034 single changes (`swing_trend` / `momentum_alpha`) | trailing 200: +30.01% / +28.00%, +27.63% / +18.05%; trailing 300: +28.16% / +29.87%, +31.41% / +17.16%; no break-even: +32.95% / +21.94%, +34.64% / +13.53%; `min_score` 0.55: +13.37% / +26.05%, +21.38% / +16.81%; 0.78: +24.92% / +23.17%, +26.50% / +13.43%; activation 7%: +8.54% / +3.03%, +10.13% / +0.90% | not adopted | **holds** |
| D-034 `swing_trend` activation 13% alone (cap 4.5%) | +45.16% / +28.15% (DD 9.72%) | design only, then combined with the cap | **wins both periods**, by 0.8 p.p. on the holdout |
| D-034 `momentum_alpha` cap 8% (+1.5 p.p.) | +32.03% / +19.53% | — | marginally better on both (+2.1 / +1.2 p.p.) |
| D-034 more risk per trade (`swing_trend` 1.5%, `momentum_alpha` 2.0%) | +43.74% / +32.35% (DD 12.10%); +34.13% / +20.66% (DD 9.86%) | rejected (worse holdout, more drawdown) | **wins both periods**: portfolio +40.09% / +27.38%, +78.44% over 15 windows, worst window −7.73% |
| D-034 new profiles | `trend_daily` +41.82% / +0.17%, and a portfolio 45/25/5/25 with it +57.84%; `breakout_alt` +8.17% / −9.44% | rejected | **holds** |
| D-035 ATR-based activation in `swing_trend` | 4 × ATR 7–16%: +5.52% / +26.55%; 5 ×: +32.51% / +19.86%; 6 ×: +51.43% / +25.83%; 5 × 9.7–13%: +43.16% / +18.10% | rejected: no variant won on the design | **holds**, but 6 × and 5 × 9.7–13% now win on the design and lose on the holdout |
| Section 2.2 `trend_atr` / first `alt_atr` | `trend_atr` fixed 15%: +8.67% / −6.21%; 2.5 × ATR: −4.65% / −1.37%; `alt_atr` fixed 11%: +42.16% / −4.04%; 4 × ATR 8–18%: +9.59% / −9.76%; 10–13%: +17.81% / −7.41% | rejected: no profit on the holdout | **holds** (the ATR-based activation no longer loses less on 4h) |
| D-036 `alt_atr` fixed 10.9% (adopted) | fixed 11%: +28.79% / −5.54%; 4 × ATR 8–18%: +21.93% / −15.57%; 5 × 8–20%: +38.85% / −4.29%; 5 × 8–16%: +52.81% / −4.72%; 6 × 8–20%: +38.75% / −6.07% | adopted: the only profit on the holdout | **holds** |
| Section 2.6, 201 vs 999 candles | `swing_trend` 201: +30.37% / +32.61%; `momentum_alpha`: +31.79% / +12.62%; `alt_atr`: +21.65% / +9.25%; portfolio +63.72% over 15 windows | 999 better for `swing_trend` and `momentum_alpha` in 2025–2026 | about even on the portfolio. 999 is better for `momentum_alpha`, and 201 for `swing_trend` and `alt_atr` in 2025–2026. D-040 rests on parity with the agent, not on this |

**What changed:** two former rejections now win on both periods, more risk per trade and the 13% activation alone in `swing_trend`, and the D-035 rejection holds for a different reason. Every adopted value (D-034 for `momentum_alpha`, D-036, and D-039's `k` in section 2.7) holds. No calibration was changed here: the 2025–2026 holdout has been used for every decision since D-034. The risk change also scales the drawdown, with `swing_trend`'s worst window at 12.10% against the 13% cap. The activation's holdout gain (0.8 p.p.) is within the noise of these runs.

**Not re-run:** the five D-039 hypotheses rejected in section 2.4 (stretch filter, pullback only, limit entry, exit at RSI ≥ 80, universe by the 30-day median volume). They ran on experimental code that was never versioned. Their margins were large, and section 2.7's corrections moved the results by up to ~4 p.p. The optimized walk-forward (section 3) is superseded.

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
| `min_stop_atr` (signals) | **1.5** (D-039) | **2.0** (D-039) |

The median uses trainings later than the first windows, so this set was **not** validated out of sample as a whole. The Demo (doc 04) is the validation that counts.

## 5. Limitations

- **Entry stop before section 2.7:** until 2026-10-24 the lab placed the stop at the cap during the entry's first 4h candle, then at that candle's own stop, instead of the signal candle's stop the agent uses. Sections 2 to 2.6 ran that way (section 2.7); section 2.8 re-runs their decisions, except the five D-039 hypotheses whose code was never versioned.
- **Survivorship bias:** the pair list is static, made of assets that exist since 2021. The real universe changes every cycle (D-027), and most of the recent setups showed up in assets outside that list. `--alts` (section 2.4) emulates the minimum 24h volume of the universe, not its *tier* ranking, and its 18 alts were picked among the ones the agent traded or saw in 2026-10.
- **Indicator history:** the agent computes the features on the last 251 candles (`warmup + 50`), so its EMA 200 is still close to the TA-Lib seed (an SMA); the lab uses the whole history. In 2025–2026, on the `swing_trend` pairs, about 9% of the entry signals differ each way (section 2.4).
- **Idealized execution:** fills at the stop price and at the candle open, with no *slippage* beyond the fee. With `--detail 5m` the order of events within the 4h candle follows 5m candles; within each 5m candle it is still approximated.
- **No LLM layer** and no Risk Guard limits.
- **Short sample:** 15 windows and about 300 trades per profile.

## 6. History

The first tests (2026-09-26) used the `conservador` (4h) and `moderado` (1h) profiles, since removed. The baseline with fixed parameters had a negative expectancy in both profiles. Losses went to the full stop while the trailing took small gains, and there were *altcoin* buys with BTC falling and breakouts against the trend. That led to the regime filter and the with-the-trend-only setups (D-016) and to the optimized *walk-forward* (D-017). On 1h, the excess of trades and the fees made the expectancy negative even with optimization, and the current profiles use 4h.
