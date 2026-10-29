# Lab — *walk-forward* results

> Result of the calibration of the profiles in use (`swing_trend` and `momentum_alpha`, 2026-10-01; revised on 2026-10-16, section 2; `alt_atr` created on 2026-10-17, section 2.3; stop-noise filter on 2026-10-22, section 2.4; re-run on 999 candles on 2026-10-22, sections 2.5 and 2.6; stop-noise filter re-checked on 999 candles on 2026-10-23, section 2.7; sections 2 to 2.6 re-run on the corrected lab on 2026-10-25, section 2.8; risk and `swing_trend` activation adopted on 2026-10-25, section 2.9; pair-local families and the lab's parity on 2026-10-28, section 2.10; the lab's stop at the agent's price on 2026-10-28, section 2.11). The full reports, window by window, are in `var/lab/` (not versioned).
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
| Simulated protection | Fixed stop `entry × (1 − min(k·ATR%, max_stop))` and a *trailing take-profit* with activation, like the native OPOCO. Until 2026-10-28 the stop and the *break-even* sat about 0.2% above the agent's prices (section 2.11) |
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

### 2.9 More risk per trade and the 13% activation in `swing_trend` (2026-10-25, D-041)

Section 2.8 found two former rejections that won on both periods: more risk per trade and the 13% activation alone in `swing_trend`. Both are adopted for review: `swing_trend` risk 1.2% → **1.5%** and activation 9.7% → **13%**, and `momentum_alpha` risk 1.5% → **2.0%**. The `swing_trend` combination was run here for the first time, and every component was run on `--alts` too. Same lab as section 2.7, 15 windows.

| Per profile (tier / `--alts`) | Design 2023–2024 | Holdout 2025–2026 | 15 windows | Worst window DD |
|---|--:|--:|--:|--:|
| `swing_trend` current | +35.25% / +42.13% | +27.37% / +19.07% | +72.27% / +69.24% | 9.78% / 9.72% |
| `swing_trend` activation 13% | +45.16% / +47.83% | +28.15% / **+17.03%** | +86.02% / +73.01% | 9.72% / 9.66% |
| `swing_trend` risk 1.5% | +43.74% / +52.32% | +32.35% / +22.52% | +90.23% / +86.63% | 12.10% / 12.03% |
| **`swing_trend` risk 1.5% + activation 13%** | **+55.26% / +58.33%** | **+32.93% / +19.37%** | **+106.39% / +89.00%** | 12.03% / 11.95% |
| `momentum_alpha` current | +29.95% / +31.10% | +18.37% / +7.73% | +53.83% / +41.23% | 7.46% / 9.79% |
| **`momentum_alpha` risk 2.0%** | **+34.13% / +35.62%** | **+20.66% / +7.16%** | **+61.84% / +45.34%** | 9.86% / **12.89%** |

| Portfolio 65 / 30 / 5% | Tier: design / holdout / 15 windows (worst window) | `--alts`: design / holdout / 15 windows (worst window) |
|---|--:|--:|
| Current | +33.24% / +23.45% / +64.49% (−6.22%) | +37.93% / +14.69% / +58.19% (−5.79%) |
| **Adopted (risk + activation 13%)** | **+47.44% / +27.80% / +88.44%** (−8.35%) | **+49.66% / +14.79% / +71.79%** (−4.44%) |
| Risk only | +40.09% / +27.38% / +78.44% (−7.73%) | +45.87% / **+16.72%** / +70.26% (−6.81%) |
| Activation 13% only | +39.59% / +23.96% / +73.04% (−6.73%) | +41.54% / +13.43% / +60.54% (−3.58%) |

**Reading:**

- **Results:** the adopted set improves the 15 windows on both baskets, and every profile's worst window stays under 13%.
- **Where the gain comes from:** the higher risk. On `--alts`, the basket that reproduces the Demo, the 13% activation lowers the holdout, both alone (+17.03% vs +19.07% in `swing_trend`) and in the portfolio (risk only +16.72% vs adopted +14.79%). On the tier lists it helps on both periods.
- **Drawdown:** more risk is leverage, so the drawdown grows with the return. `momentum_alpha` on `--alts` reaches 12.89%, at the cap.
- **What the lab does not simulate:** the global circuit breakers (`config/stop_conditions.yaml`). Four stops in one day (two per profile at their caps) cost about 2.5% of the managed capital before and **about 3.2% now**, above the 3% daily-loss pause. The 15% maximum drawdown halt is further away, since the worst window of the portfolio is 8.35%.
- **Holdout:** the 2025–2026 holdout has been used for every decision since D-034, so none of this is out of sample. The Demo is the validation.
- **At the agent's stop (section 2.11):** the adopted set still beats the profiles it replaced on both baskets, but on `--alts` `momentum_alpha`'s worst window rises to 13.28% and the 13% activation lowers the 15 windows.

### 2.10 Pair-local families (2026-10-28, D-044)

**Question.** Since 2026-10-22 BTC closes below its 4h EMA 200, and the regime filter (D-016) leaves every profile without a setup. Can a profile whose entries and exits read only the pair's own candles find an edge after costs, also with BTC below its EMA 200? Three families were implemented with the research seeds of the specification, frozen before the runs (`trade_agent/signals/local.py`): `pair_pullback_rsi`, `pair_volatility_breakout` and `pair_range_reversion`. `config/profiles.yaml` carries them disabled (`pair_pullback_1h`, `pair_breakout_1h`, `pair_range_1h`). Their risk budget runs in the production Risk Guard with the lab's code (D-044).

**Lab parity first.** The lab now receives every value of the profile: the Freqtrade parameters, which *hyperopt* may change, and `trade_agent_profile.json` with everything else. That covers all the signal parameters of the family (before, `use_regime_filter`, `rsi_overbought` and `min_stop_pct` never reached the strategy), the local take-profit options, the scope's risk budget and the capital model. The strategy writes back the values it used, and every window checks them (`check_effective`). Errors in the callbacks Freqtrade swallows go to an events file the lab checks. The first runs found four silent problems, all fixed:

1. Freqtrade calls `bot_start` before loading the parameter file: a strategy reading its parameters there would use the defaults.
2. Freqtrade rounds a loaded value to the parameter's `decimals`: a 0.25% risk per trade ran as 0.3% (`risk_per_trade` now has 4 decimals). The legacy values were not affected.
3. With `--detail 5m`, Freqtrade retries a refused entry on every 5m candle of the next candle, so it could buy later at another price. The agent sends the entry once, right after the close, and so does the lab now for the pair-local families. The legacy strategy never refuses an entry in `confirm_trade_entry`.
4. Freqtrade reads `after_fill` from `custom_stoploss`'s signature (`getfullargspec`, blind to decorators). A logging decorator there made it skip the stop placed after the fill, so every stop was labelled `trailing_stop_loss`, and the scope's re-entry wait after a stop never applied. A test now guards the signature.

**Nothing changed for the legacy profiles.** A snapshot of `swing_trend`, `momentum_alpha` and `alt_atr`, computed on the base commit (features, score, setups, stops, entry and exit masks, protection policy and an entry plan, on seven series), is reproduced bit for bit (`tests/unit/signals/test_legacy_regression.py`). Group A below ran on the lab before and after this change, and the six walk-forwards export the same trades: the same entries, exits, prices, sizes and results.

**Independence from BTC and history.** For each family, moving BTC above or below its EMA 200, crashing it or removing it changes no score, setup, stop, target or technical exit (`tests/unit/signals/test_local.py`, and `tests/integration/decision/test_local_engine.py` in the engine, which goes on without the benchmark's candles). On the lab's candles of 8 pairs (BTC, ETH, SOL, LINK, ATOM, OP, INJ and MAGIC, the last two listed in 2022-10), every setup and 40 random candles per pair and family give the same setup and stop on the agent's 999 candles as on the whole history: 2,581 candles checked on 1h and 4h, no difference. In the image, every exported trade of a pullback and a breakout run was checked against the family's own frames (`tests/integration/lab/test_lab_families.py`): entries only at the open of the candle after a setup, fixed targets filled at their price (a native limit order, Freqtrade's ROI), stops at the signal candle's stop (with the bias below, fixed in section 2.11), and technical exits only after a closed candle with the rule.

**Open finding, fixed in section 2.11: the lab's stop sat ~0.2% above the agent's.** Freqtrade passes `custom_stoploss` a profit net of the fees, and `stoploss_from_open` reads it as a price ratio. The stop then lands at `entry × (1 − stop) × (1 + fee) ÷ (1 − fee)`, rounded up to the tick: with the 0.1% fee, about 0.2% above the stop the agent's OCO places (a 4.5% stop behaves as about 4.3%). It concerns every profile and every result since D-014, the current profiles' included. The tables of this section ran with it; section 2.11 re-runs them at the agent's price.

**Method (fixed before the runs).**

| Group | What | How |
|---|---|---|
| A | The three current profiles, unchanged | 15 quarterly windows 2023-01 → 2026-09, `--detail 5m`, wallet sizing (the documented method of sections 2.7–2.9) |
| B | A with `use_regime_filter: false` | as A |
| C | B with the local score (`local_score: true`: the score without the relative strength, which then tops out at 0.80) at `min_score` 0.52, 0.57, 0.62 and 0.67 | as A; 0.67 is the mechanical removal, and the threshold is also re-chosen on the previous 4 windows (windows 5–15) |
| A_op | A sized on the configured capital | one continuous backtest 2023-01 → 2026-09, marked to market |
| D | Each family on 1h and on 4h (the same rules in candles: the 4h maximum holding is 4× the 1h one in hours), with the scope's whole budget | continuous, on 100 USDT of virtual capital, sized on that capital |
| E | The 1h families without the scope's pauses (`no_pauses`), then one component changed at a time | continuous, as D |

Every run is on the tier lists and on `--alts`. The 1h candles of the 18 `--alts` pairs were downloaded on 2026-10-28 (public Binance klines). The continuous runs keep positions across the quarters. The equity is marked to market on each candle close, the quarters are cut from it, and the report adds the costs of 5, 10 and 20 extra bps per side to both legs of every trade. On the configured capital that matches a run with a higher fee, except for the cash left for the next entry. The BTC regime at the entry is the one the agent saw: BTC's close against its EMA 200 on the profile's timeframe, on the last candle closed at the entry. It is for auditing only. `lab/report.py` computes every metric; the full reports are in `var/lab/` (not versioned).

```bash
uv run python -m lab.experiments --list                     # the 82 runs
uv run python -m lab.experiments --only A B C A_op D E --jobs 4 --resume
uv run python -m lab.report --out var/lab/report-d044.json --markdown var/lab/report-d044.md
```

A continuous 1h run on `--alts` takes up to ~2.7 GB of Docker's memory, which the production containers share: keep `--jobs` within it.

**What it is not.** 2023-01 → 2026-09 was already used to select the current profiles, so it is no clean holdout. Nothing here was calibrated on it, but every result is **exploratory**. Freqtrade fills at the stop, target or candle open with no slippage; the extra costs and the 0.3% round-trip cost in the sizing stand in for it. The universe is the static tier lists plus 18 alts picked among the ones the agent traded or saw in 2026-10 (survivorship and selection bias), with the 24h volume approximated by `close × volume`. Neither the global stop conditions, the tier limits nor the LLM are simulated.

**A, B and C: the current profiles without the BTC regime filter or the relative strength** (15 windows, compounded, tier lists / `--alts`; worst window drawdown marked to market):

| Profile | A (as today) | B (filter off) | C (filter off, no relative strength, `min_score` 0.67) | Worst window DD A | Worst window DD B | B: entries with BTC below its EMA 200 | Their net (USDT) |
|---|--:|--:|--:|--:|--:|--:|--:|
| `swing_trend` | +106.41% / +89.00% | +87.82% / +79.13% | +16.35% / −3.88% | 14.60% / 14.52% | 14.60% / 14.52% | 30 / 34 | +43.69 / +46.07 |
| `momentum_alpha` | +61.83% / +45.36% | +54.40% / +33.96% | +15.42% / +6.21% | 10.67% / 13.38% | 12.31% / 17.43% | 24 / 30 | +11.56 / +9.78 |
| `alt_atr` | +23.24% / +20.55% | +19.82% / +17.36% | +25.53% / +26.19% | 6.75% / 6.78% | 7.05% / 7.05% | 35 / 37 | +1.66 / +1.67 |

- **B:** without the filter every profile makes less, on both baskets. The entries it adds with BTC below its EMA 200 make money in themselves, but they take slots and cash that the later entries with BTC above it would have used.
- **C:** at 0.52, 0.57 and 0.62 the runs are identical (with the local score a legacy setup scores 0.60 or more, or below 0.52), so C has two points: 0.52 and 0.67. At 0.67, the mechanical removal, `swing_trend` and `momentum_alpha` lose most of their result; `alt_atr` gains a little with a third of the trades. Re-choosing the threshold on the 4 windows before each window (windows 5–15, compounded, tier / `--alts`) gives:
  - `swing_trend`: +19.63% / +1.61%, against A's +85.34% / +58.38%;
  - `momentum_alpha`: +21.97% / +11.17%, against +36.16% / +18.29%;
  - `alt_atr`: +6.55% / +11.96%, against +11.53% / +9.80%.

  What the regime filter and the relative strength select is most of what makes these profiles work.
- **The worst windows' drawdown, marked to market,** reaches 14.60% in `swing_trend`. Freqtrade's figure in sections 2.7–2.9 (12.03%) only counted closed trades.

**A_op: the current profiles as one continuous run sized on the configured capital** (tier / `--alts`):

| Profile | Trades | Return | Max DD (MTM) | Recovery (days) | PF | Expectancy in R [5%; 95%] | With +20 bps per side |
|---|--:|--:|--:|--:|--:|--:|--:|
| `swing_trend` | 176 / 205 | +89.84% / +80.00% | **28.02% / 27.18%** | 314 / 314 | 1.64 / 1.46 | 0.37 [0.09; 0.64] / 0.28 [0.04; 0.50] | +65.20% / +51.62% |
| `momentum_alpha` | 181 / 219 | +57.77% / +44.30% | 15.03% / 19.98% | 325 / 325 | 1.36 / 1.21 | 0.20 [0.01; 0.35] / 0.13 [−0.03; 0.25] | +32.72% / +14.92% |
| `alt_atr` | 180 / 184 | +20.50% / +17.54% | 11.69% / 13.39% | 328 / 328 | 1.24 / 1.20 | 0.11 [−0.09; 0.28] / 0.09 [−0.10; 0.26] | +5.41% / +2.23% |

As the specification warned, the worst quarter is no substitute for the whole path: over the continuous run `swing_trend`'s drawdown is about twice its worst window's. These runs are the reference of D; nothing in the current profiles was changed.

**D: the families, with the scope's whole budget** (continuous, 100 USDT, tier / `--alts`):

| Family | Trades | Return | Max DD (MTM) | PF | Win rate | Expectancy in R [5%; 95%] | Entries with BTC below EMA 200 | Their R | With +20 bps |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| `pair_pullback_rsi` 1h | 21 / 25 | +2.09% / +3.09% | 2.03% / 1.42% | 1.64 / 1.82 | 42.9% / 44.0% | 0.25 [−0.19; 0.57] / 0.29 [0.02; 0.59] | 2 / 2 | 0.03 / 0.03 | +0.28% / +1.12% |
| `pair_pullback_rsi` 4h | 1 / 1 | +0.19% / +0.19% | 0.51% / 0.51% | — | — | — | 0 / 0 | — | +0.14% / +0.14% |
| `pair_volatility_breakout` 1h | 113 / 149 | −3.43% / −3.77% | 8.11% / 8.14% | 0.85 / 0.87 | 15.0% / 14.1% | −0.08 [−0.26; 0.08] / −0.05 [−0.19; 0.10] | 36 / 40 | −0.28 / −0.16 | −15.14% / −18.03% |
| `pair_volatility_breakout` 4h | 157 / 169 | −1.37% / +5.09% | 6.79% / 5.18% | 0.95 / 1.16 | 14.0% / 16.0% | −0.02 [−0.20; 0.16] / 0.06 [−0.11; 0.23] | 27 / 25 | 0.04 / 0.07 | −10.24% / −4.06% |
| `pair_range_reversion` 1h and 4h | 0 | 0% | — | — | — | — | — | — | — |

On 1h the breakout reached the 8% drawdown and the review stopped its entries for the rest of the run (about 1,000 refused). The other pauses fired too: losing streaks 27–41 times in the breakout, 1–2 in the pullback. The baskets' *buy & hold* over the period: +116.85% (tier lists) and +60.21% (`--alts`); BTC +375.05%. Cash: 0%.

**E: the 1h families without the scope's pauses, one component at a time** (tier / `--alts`):

| Variant | Trades | Return | Max DD (MTM) | PF | Expectancy in R [5%; 95%] | Entries with BTC below EMA 200 | Their R |
|---|--:|--:|--:|--:|--:|--:|--:|
| pullback, no pauses (base of E) | 21 / 25 | +2.09% / +3.09% | 2.03% / 1.42% | 1.64 / 1.82 | 0.25 [−0.19; 0.57] / 0.29 [0.02; 0.59] | 2 / 2 | 0.03 / 0.03 |
| without ADX | 33 / 39 | +1.17% / +2.53% | 3.21% / 2.47% | 1.21 / 1.38 | 0.10 [−0.23; 0.30] / 0.15 [−0.11; 0.36] | 5 / 6 | −0.20 / −0.29 |
| without the extension filter | 21 / 26 | +2.09% / +2.86% | 2.03% / 1.42% | 1.64 / 1.72 | 0.25 [−0.19; 0.57] / 0.26 [0.02; 0.52] | 2 / 2 | 0.03 / 0.03 |
| without the volume | 26 / 30 | +2.27% / +3.26% | 1.56% / 1.41% | 1.59 / 1.75 | 0.21 [−0.07; 0.43] / 0.25 [0.05; 0.47] | 2 / 2 | 0.03 / 0.03 |
| OBV instead of the volume | 6 / 9 | +1.90% / +2.97% | 0.57% / 0.94% | 6.59 / 4.73 | 0.74 / 0.73 | 0 / 0 | — |
| RSI dip ≤ 45 (instead of 40) | 135 / 179 | −0.57% / −0.93% | 4.96% / 4.09% | 0.98 / 0.98 | −0.01 [−0.13; 0.11] / −0.01 [−0.10; 0.08] | 24 / 28 | −0.13 / −0.11 |
| trailing from 2.5R, 1 ATR | 21 / 25 | +3.30% / +3.86% | 2.16% / 1.48% | 2.01 / 1.92 | 0.38 [−0.16; 0.82] / 0.37 [0.07; 0.68] | 2 / 2 | 0.03 / 0.03 |
| breakout, no pauses (base of E) | 780 / 910 | −39.80% / −40.10% | 41.20% / 41.44% | 0.74 / 0.77 | −0.10 [−0.17; −0.04] / −0.09 [−0.15; −0.03] | 174 / 207 | −0.06 / −0.12 |
| without the extension limit | 804 / 932 | −37.52% / −38.73% | 40.06% / 40.54% | 0.76 / 0.79 | −0.09 [−0.16; −0.03] / −0.08 [−0.14; −0.02] | 188 / 220 | −0.07 / −0.13 |
| without the true range limit | 888 / 1,046 | −29.84% / −32.59% | 34.62% / 35.53% | 0.83 / 0.84 | −0.07 [−0.13; −0.01] / −0.06 [−0.12; −0.00] | 201 / 247 | −0.01 / −0.07 |
| ADX ≥ 20 or rising | 757 / 893 | −38.44% / −40.14% | 39.87% / 41.52% | 0.74 / 0.77 | −0.10 [−0.16; −0.04] / −0.09 [−0.15; −0.03] | 165 / 199 | −0.06 / −0.13 |
| entry on the retest | 502 / 587 | −27.47% / −29.00% | 31.79% / 34.15% | 0.76 / 0.78 | −0.12 [−0.20; −0.04] / −0.11 [−0.18; −0.03] | 93 / 116 | −0.10 / −0.11 |
| trailing 1.5 ATR | 776 / 901 | −39.57% / −38.44% | 40.95% / 39.80% | 0.73 / 0.78 | −0.11 [−0.17; −0.04] / −0.09 [−0.15; −0.03] | 174 / 207 | −0.06 / −0.12 |
| range, no pauses | 0 / 0 | 0% | — | — | — | — | — |

The pullback's pauses never stopped it, so its E base equals D. In the breakout, 525 of the 780 exits were the technical one (a close back below the level) and the win rate was 13%. Its average MFE was 1.9% against an MAE of −1.1%. It loses on both baskets, on 11 and 13 of the 15 quarters and in both BTC regimes.

**Verdicts.** These are section 11's criteria, fixed before the runs: positive net expectancy with an interval that excludes zero, profit factor ≥ 1.15, a conservative planned R:R ≥ 1.5, the scope's drawdown within 8%, a positive or clearly bounded result in the entries with BTC below its EMA 200, and no dependence on one pair, one trade or a narrow parameter.

| Family | Verdict | Why |
|---|---|---|
| `pair_pullback_rsi` | **Inconclusive** | Positive on both baskets (PF 1.64 / 1.82, robust to +20 bps), but with 21 and 25 trades in 44 months, ~1% of the time in position and +0.6% to +0.8% a year on the profile's capital. The interval of the expectancy includes zero on the tier lists, and without its 5 best trades the result is negative. **Only 2 entries had BTC below its EMA 200**, so the central hypothesis was not tested. The one change that brings enough trades (an RSI dip ≤ 45) has no edge (PF 0.98, −0.13 R with BTC below its EMA 200): the result sits on a narrow point of the parameter. 4h: 1 trade |
| `pair_volatility_breakout` | **Rejected** | Without the pauses, negative expectancy with the whole interval below zero on both baskets and in every variant (PF 0.73–0.84, drawdown 32–42%). With the budget, the 1h run hit the 8% drawdown review. It loses with BTC below its EMA 200 too. The only positive run (4h on `--alts`, +5.09%) is negative without its 5 best trades and at +20 bps |
| `pair_range_reversion` | **Rejected** | No trade. Over 44 months the pattern appears 14 times on 1h and once on 4h across both baskets, and the middle band is always too close: net R:R median 0.62, maximum 1.34, never the 1.5 the Risk Guard requires. The specification forbids lowering that floor |

**F (combined portfolio) was not run:** no family is fit for the next phase, so there is no candidate to combine. **No prospective paper trading either:** that phase was for a family fit for it, and its virtual portfolio would need its own account and state in production (§6 of the specification).

**Economic result.** The families run without the LLM (no research cost) and would share the agent's infrastructure (no incremental cost measured). The trading results above are after Freqtrade's 0.1% fee per side; the 0.3% round-trip cost of the sizing is not deducted again.

**Limitations of this section.**

- Groups B and C ran before the fourth parity fix. Their stops are labelled `trailing_stop_loss` in the exports, but their prices, sizes and results are not affected: group A ran before and after the fix with identical trades.
- The continuous runs take the fixed sizing of the configured capital, without reinvesting.
- The bootstrap resamples the daily returns in 10-day blocks and cannot create regimes the period does not have.
- No family that kept a positive expectancy reached the ~100 trades and 30 entries with BTC below its EMA 200 that the specification asks of a prospective test.

### 2.11 The lab's stop at the agent's price (2026-10-28)

**What changed.** Since D-014 the lab converted the profile's stop with `stoploss_from_open` on the profit Freqtrade passes `custom_stoploss`. That profit is net of both fees, so the stop landed at `entry × (1 − stop) × (1 + fee) ÷ (1 − fee)`, rounded up to the tick. With the 0.1% fee that is about 0.2% above the agent's OCO stop: a 4.5% stop filled at about 4.3%. The *break-even* had the same bias (`entry × 1.003` sat at about `entry × 1.005`). The strategy now places both at the agent's absolute prices (`stoploss_from_absolute`). Two tests guard it:

- `tests/integration/lab/test_lab_causality.py` reads the stop price Freqtrade places from the strategy's ratio, called as the backtest calls it.
- The families' fidelity test bounds every stop fill by the agent's stop plus one tick. Freqtrade rounds the stop up to a historic tick it derives from the month's prices; the agent rounds down, so the two can differ by one tick.

Freqtrade still fills at the stop price, without the slippage of the agent's stop-market leg.

**Why it moves the results.** The size is the risk ÷ the stop, so the bias made each stop lose less than the planned risk. A stop exit now gives up 0.2 p.p. more of the price: −4.13% → −4.32% on average in `swing_trend` (tier lists), −5.64% → −5.84% in `momentum_alpha`. That is about 4% of R on a 4.5% stop and up to 20% on the families' 1% stops. A few trades that touched the old stop now go on, so there are slightly fewer stop exits.

**Section 2.9 (D-041) at the agent's stop.** Same method and lab as section 2.9, 15 windows; its tables have the numbers before the fix.

| Per profile, after (tier / `--alts`) | Design 2023–2024 | Holdout 2025–2026 | 15 windows | Worst window DD | Before: 15 windows |
|---|--:|--:|--:|--:|--:|
| `swing_trend` before D-041 | +29.65% / +44.68% | +25.14% / +16.66% | +62.26% / +68.77% | 10.18% / 10.15% | +72.27% / +69.24% |
| `swing_trend` activation 13% | +40.22% / +46.81% | +25.79% / **+14.59%** | +76.38% / **+68.23%** | 10.16% / 10.13% | +86.02% / +73.01% |
| `swing_trend` risk 1.5% | +36.32% / +55.74% | +29.54% / +19.48% | +76.59% / +86.07% | 12.59% / 12.56% | +90.23% / +86.63% |
| **`swing_trend` risk 1.5% + activation 13% (current)** | **+48.77% / +56.85%** | **+29.92% / +16.28%** | **+93.28% / +82.39%** | 12.57% / 12.53% | +106.39% / +89.00% |
| `momentum_alpha` before D-041 (risk 1.5%) | +30.38% / +28.33% | +16.49% / +5.66% | +51.87% / +35.60% | 6.93% / 10.09% | +53.83% / +41.23% |
| **`momentum_alpha` risk 2.0% (current)** | **+34.93% / +31.97%** | **+18.18% / +4.55%** | **+59.47% / +37.97%** | 9.22% / **13.28%** | +61.84% / +45.34% |
| `alt_atr` (current) | +16.57% / +17.04% | +5.83% / +3.17% | +23.36% / +20.75% | 6.56% / 6.53% | +23.26% / +20.56% |

| Portfolio 65 / 30 / 5%, after | Tier: design / holdout / 15 windows (worst window) | `--alts`: design / holdout / 15 windows (worst window) | Before: 15 windows (tier / `--alts`) |
|---|--:|--:|--:|
| Before D-041 | +29.67% / +21.56% / +57.63% (−6.60%) | +38.65% / +12.62% / +56.14% (−6.17%) | +64.49% / +58.19% |
| **Current (risk + activation 13%)** | **+43.55% / +25.23% / +79.76%** (−8.81%) | **+47.55% / +12.11% / +65.42%** (−4.95%) | +88.44% / +71.79% |
| Risk only | +35.51% / +24.94% / +69.30% (−8.18%) | +46.86% / **+14.07% / +67.52%** (−7.27%) | +78.44% / +70.26% |
| Activation 13% only | +36.51% / +21.98% / +66.51% (−7.12%) | +39.99% / +11.33% / +55.85% (−4.01%) | +73.04% / +60.54% |

- **D-041 still beats the profiles it replaced** over the 15 windows on both baskets (portfolio +57.63% → +79.76% on the tier lists, +56.14% → +65.42% on `--alts`), and the higher risk is still where the gain comes from.
- **Two of its premises no longer hold on `--alts`:**
  - `momentum_alpha` at 2.0% risk has a 13.28% worst window, above the 13% cap sections 2.7 and 2.9 chose by (12.89% before the fix). At 1.5% it is 10.09%.
  - The 13% activation in `swing_trend` now lowers the 15 windows on `--alts` too, not only the holdout: alone +68.23% against +68.77%, and in the portfolio the current set makes +65.42% against risk only's +67.52%. On the tier lists it still helps on both periods, by 0.3 p.p. in the portfolio's holdout, within the noise. On `--alts` the current set's holdout (+12.11%) is now below the one before D-041 (+12.62%).
- **The operator's choice (D-045, PR #41):** `momentum_alpha`'s risk goes back to 1.5%, and `swing_trend` keeps D-041. The 13% activation stays, since it wins the design on both baskets and section 2.8's rule keeps the current value unless another one does.

**A_op at the agent's stop** (one continuous run sized on the configured capital, section 2.10; return and drawdown before → after, the other columns after):

| Profile (tier / `--alts`) | Trades | Return | Max DD (MTM) | PF | Expectancy in R [5%; 95%] | With +20 bps per side |
|---|--:|--:|--:|--:|--:|--:|
| `swing_trend` | 174 / 204 | +89.84% → **+81.99%** / +80.00% → **+77.64%** | 28.02% → **29.98%** / 27.18% → **29.14%** | 1.58 / 1.44 | 0.34 [0.05; 0.63] / 0.27 [0.03; 0.51] | +57.63% / +49.41% |
| `momentum_alpha` | 179 / 217 | +57.77% → **+55.82%** / +44.30% → **+37.97%** | 15.03% → 15.12% / 19.98% → **21.14%** | 1.34 / 1.18 | 0.19 [0.00; 0.34] / 0.11 [−0.05; 0.23] | +31.02% / +8.86% |
| `alt_atr` | 180 / 184 | +20.50% → +20.53% / +17.54% → +17.49% | 11.69% → 11.99% / 13.39% → 12.51% | 1.23 / 1.19 | 0.12 [−0.08; 0.29] / 0.10 [−0.09; 0.27] | +5.44% / +2.19% |

**A, B and C at the agent's stop** (section 2.10's table, 15 windows compounded, tier / `--alts`; before → after, drawdowns after):

| Profile | A (as today) | B (filter off) | C (filter off, no relative strength, `min_score` 0.67) | Worst window DD A | Worst window DD B | B: entries with BTC below its EMA 200 (net USDT) |
|---|--:|--:|--:|--:|--:|--:|
| `swing_trend` | +106.41% → **+93.29%** / +89.00% → **+82.39%** | +87.82% → **+78.12%** / +79.13% → **+74.95%** | +16.35% → **+18.36%** / −3.88% → **−1.29%** | 15.12% / 15.09% | 15.12% / 15.09% | 28 / 32 (+50.00 / +51.03) |
| `momentum_alpha` | +61.83% → **+59.48%** / +45.36% → **+37.98%** | +54.40% → **+53.23%** / +33.96% → **+27.96%** | +15.42% → **+20.12%** / +6.21% → **+6.42%** | 10.95% / 13.76% | 12.78% / 17.99% | 23 / 29 (+13.45 / +11.24) |
| `alt_atr` | +23.24% → **+23.34%** / +20.55% → **+20.75%** | +19.82% → **+22.50%** / +17.36% → **+19.94%** | +25.53% → **+23.03%** / +26.19% → **+24.85%** | 6.99% / 7.01% | 7.28% / 7.28% | 33 / 35 (+2.68 / +2.68) |

- **B:** without the filter every profile still makes less over the 15 windows, on both baskets. The margin is wide in `swing_trend` and `momentum_alpha`, but under 1 p.p. in `alt_atr` (it was 3.4 / 3.2 p.p.). On windows 5–15, `momentum_alpha` without the filter now makes slightly more than with it: +34.60% / +18.29% against +34.01% / +15.45%.
- **C:** the reading holds. At 0.67 `swing_trend` and `momentum_alpha` lose most of their result; `alt_atr` now matches A on the tier lists (+23.03% vs +23.34%) and still beats it on `--alts` (+24.85% vs +20.75%), with about half the trades (86 / 93 against 180 / 184). Re-choosing the threshold on the 4 windows before each window (windows 5–15, tier / `--alts`; before the fix in parentheses):
  - `swing_trend`: +15.60% / −1.09% against A's +78.24% / +57.76% (+19.63% / +1.61% against +85.34% / +58.38%);
  - `momentum_alpha`: +31.11% / +10.22% against +34.01% / +15.45% (+21.97% / +11.17% against +36.16% / +18.29%);
  - `alt_atr`: +6.12% / +10.86% against +12.64% / +11.02% (+6.55% / +11.96% against +11.53% / +9.80%).

**The pair-local families (section 2.10, D and E) at the agent's stop.** The verdicts hold:

- `pair_pullback_rsi` 1h: 21 / 25 trades, +2.19% / +3.17% (was +2.09% / +3.09%), PF 1.69 / 1.86, expectancy 0.26 R [−0.15; 0.57] / 0.29 R [0.04; 0.59]. Still **inconclusive**: few trades, an interval that includes zero on the tier lists, and the same two entries with BTC below its EMA 200. Its stops are wide and rare (1 and 2 stop exits), so the fix barely touches it.
- `pair_volatility_breakout`: worse, since its 1% stops are where the bias weighed most. Without the pauses −41.12% / −42.45% (was −39.80% / −40.10%), PF 0.73 / 0.76, expectancy −0.11 R [−0.17; −0.05] / −0.10 R [−0.16; −0.03]. Every E variant stays below zero. Still **rejected**.
- `pair_range_reversion`: still no trade. Still **rejected**.

**Reading.**

- **The lab was optimistic, and still is.** Over the 15 windows `swing_trend` loses 13 p.p. on the tier lists and 7 on `--alts`, `momentum_alpha` 2 and 7; `alt_atr`, whose stops are wider, hardly moves. Freqtrade still fills at the stop price and at the candle open with no slippage, so the agent's results should sit lower still.
- **Drawdowns.** As one continuous run, `swing_trend` reaches 30.0% / 29.1% and `momentum_alpha` 15.1% / 21.1%. The worst windows marked to market reach 15.12% in `swing_trend`.
- **Older sections.** Sections 2 to 2.8 compare variants run on the same biased lab, so the bias moves both sides of each comparison. Here the gap between two variants of one profile moved by up to ~6 p.p. over the 15 windows (`swing_trend`'s activation on `--alts`), and the worst windows by up to ~0.6 p.p. The choices of D-034, D-036 and D-039 in sections 2.7 and 2.8 rest on margins of about 8 p.p. or more on the period they were chosen by, or reject drawdowns 3 p.p. or more above the cap, so they were not re-run; only a re-run would settle them. Section 3 is superseded.

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
| `activation_pct` (TP) | **13.0** (D-041; was 9.7) | **13.0** (D-034; was 9.5) |
| `risk_per_trade_pct` | **1.5** (D-041; was 1.2) | **1.5** (D-045; D-041 had 2.0) |
| `trailing_delta_bps` (TP) | 100 | 130 |
| `min_stop_atr` (signals) | **1.5** (D-039) | **2.0** (D-039) |

The median uses trainings later than the first windows, so this set was **not** validated out of sample as a whole. The Demo (doc 04) is the validation that counts.

## 5. Limitations

- **Entry stop before section 2.7:** until 2026-10-24 the lab placed the stop at the cap during the entry's first 4h candle, then at that candle's own stop, instead of the signal candle's stop the agent uses. Sections 2 to 2.6 ran that way (section 2.7); section 2.8 re-runs their decisions, except the five D-039 hypotheses whose code was never versioned.
- **Stop before section 2.11:** until 2026-10-28 the lab's fixed stop and *break-even* sat `(1 + fee) ÷ (1 − fee)` above the agent's prices, about 0.2%. Sections 2 to 2.10 ran that way; section 2.11 re-runs sections 2.9 and 2.10.
- **Survivorship bias:** the pair list is static, made of assets that exist since 2021. The real universe changes every cycle (D-027), and most of the recent setups showed up in assets outside that list. `--alts` (section 2.4) emulates the minimum 24h volume of the universe, not its *tier* ranking, and its 18 alts were picked among the ones the agent traded or saw in 2026-10.
- **Indicator history:** the agent computes the features on the last 251 candles (`warmup + 50`), so its EMA 200 is still close to the TA-Lib seed (an SMA); the lab uses the whole history. In 2025–2026, on the `swing_trend` pairs, about 9% of the entry signals differ each way (section 2.4).
- **Idealized execution:** fills at the stop price and at the candle open, with no *slippage* beyond the fee. With `--detail 5m` the order of events within the 4h candle follows 5m candles; within each 5m candle it is still approximated.
- **No LLM layer** and no Risk Guard limits.
- **Short sample:** 15 windows and about 300 trades per profile.

## 6. History

The first tests (2026-09-26) used the `conservador` (4h) and `moderado` (1h) profiles, since removed. The baseline with fixed parameters had a negative expectancy in both profiles. Losses went to the full stop while the trailing took small gains, and there were *altcoin* buys with BTC falling and breakouts against the trend. That led to the regime filter and the with-the-trend-only setups (D-016) and to the optimized *walk-forward* (D-017). On 1h, the excess of trades and the fees made the expectancy negative even with optimization, and the current profiles use 4h.
