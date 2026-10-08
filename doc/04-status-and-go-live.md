# 04 — Current status and *go-live*

> Status on 2026-10-03: what has been validated, what is missing before trading real capital, how the project is tested, what it costs and the known risks.

## 1. Status

Every part of the architecture (doc 03) is implemented: exchange and execution, persistence and recovery, universe, signals, profiles and lab, LLM analyst, risk and Telegram, and observability.

The agent has been **paper trading in Demo Mode** since 2026-09-29, with the order lock on (`TA_TRADING_ENABLED=true`): the orders are real on Demo. The machine is a Windows box with Rancher Desktop, not a VPS yet. The profiles in use are `swing_trend` and `momentum_alpha` (doc 03, §8).

| Validation | Result |
|------------|--------|
| Order types (spike) | 19/19 on the Spot Testnet, 2026-09-28 (doc 01, §3) |
| *Live* test (open, arm, adjust, close) | passed on the Spot Testnet |
| OPOCO on Demo | running since 2026-09-29; the protected quantity is the one received after the fee (doc 01, §3) |
| Chaos tests (crash after the intent and before sending, after sending and before recording, during an adjustment; exit and expired protection with the agent down; repeated restarts) | passed in the integration suite (`tests/integration/test_chaos.py`) |
| Lab | `swing_trend` and `momentum_alpha` with a positive out-of-sample expectancy, below BTC *buy & hold* ([lab-results.md](lab-results.md)) |
| Analyst evaluation | 31/31 cases, 100% of outputs valid against the schema, 2026-09-28 (doc 03, §7.5) |
| Telegram alerts | in use: risk state changes, entries and exits, and the 3 Grafana rules |

**Demo up to 2026-10-03:** 5 positions. Four closed at the stop: two from the old `conservador` profile (MOVRUSDT, 2026-10-01) and two from `momentum_alpha` (GTCUSDT, 2026-10-02). One is open and protected (SUPERUSDT, `momentum_alpha`, since 2026-10-03). The 4 consecutive losses triggered the 12h global pause, which led to D-026.

## 2. *Go-live* checklist

- [ ] **4 weeks of Demo** (until about 2026-10-27) with **no** duplicate order and no `unprotected` position for more than 30 s.
- [ ] Reconciliation without unexplained mismatches.
- [ ] Maximum *drawdown* and daily loss within each profile's limits.
- [ ] Risk-adjusted result, after fees, compared with BTC *buy & hold* and with a "pure TA" clone of each profile (`llm.weight = 0`), with an explicit decision on the LLM's weight. **The clone does not exist yet.**
- [ ] Every circuit breaker tested; `/halt` and `/flatten` tested from the phone.
- [ ] External heartbeat configured (`TA_HEALTHCHECK_URL`, empty today) and tested with the machine off.
- [ ] Agent on a VPS in an allowed region, with at least one restart with open positions.
- [ ] Production key with withdrawals disabled, IP-restricted and Spot only.
- [ ] Automated, encrypted, off-machine backup, with a tested restore (today only the runbook's manual `pg_dump`).
- [ ] Runbook reviewed.

After the *go-live*: start with one profile and small capital (an amount you accept losing entirely) and increase it in steps, only after weeks within the risk limits at each step.

## 3. Testing strategy

| Layer | What it tests | How |
|-------|---------------|-----|
| Unit | Rounding and filters, sizing, OPOCO and OCO building, state machine, stop conditions, `MarketView` schema | `pytest`, with property-based tests (Hypothesis): "no order violates a filter", "risk ≤ limit" |
| Integration | Complete flows of execution, recovery, risk, decision, Telegram and analyst | In-memory simulated Binance (D-005), PostgreSQL in a container, simulated Bot API and Claude API |
| Chaos | Recovery and idempotency | The process dies at critical points (after the intent, after sending, during an adjustment), the position changes with the agent down, and the startup repeats. Network, clock, 418/429, invalid LLM JSON, partial fills and a second instance have their own tests in the layers above |
| Deployment | Compose, Grafana, Loki and Alloy, Jaeger, SigNoz | Real pipelines and queries in containers; dashboard generators compared with the versioned JSON |
| *Live* | Order types and the lifecycle of a position | `pytest -m live`, against Testnet or Demo |
| Lab | The technical part (setups and parameters per profile) | Freqtrade, *walk-forward* with fees (D-017) |
| LLM evaluation | Quality and safety of the `MarketView` | 31 labeled cases (D-021) |
| *Forward test* | The complete system | Demo Mode (in progress) |

100% line and branch coverage of `src/` (D-004), with `ruff` and `mypy` clean.

## 4. Costs (monthly)

| Item | Amount | Note |
|------|--------|------|
| Claude API | US$ 15–130 | Observed on Demo: from US$ 0.52 to 4.25 per day (2026-09-29 to 2026-10-03). Cap of US$ 5/day (`config/research.yaml`) |
| VPS | US$ 20–40 | Not contracted yet; with SigNoz, it needs at least 8 GB of RAM (doc 03, §14) |
| Healthchecks.io, Grafana, Loki, Jaeger, SigNoz, Postgres | US$ 0 | Free tier and open source software |
| Trading fees | Variable | Typically 0.1% per side on a regular account. **It is the main cost of an active strategy** |

## 5. Risks

| Risk | Prob. | Impact | Mitigation |
|------|-------|--------|------------|
| Strategy without a real edge after fees | High | High | Out-of-sample *walk-forward*, Demo before production, gradual *go-live*; "do not trade" is a valid outcome |
| *Overfitting* in *hyperopt* | High | High | *Walk-forward*, median of the trainings and out-of-sample validation (the entry parameters vary a lot between trainings; [lab-results.md](lab-results.md)) |
| Unprotected position (partial fill, adjustment window, *price range*) | Low | High | FOK in both profiles, sell *fail-safe*, Grafana's "Posição sem proteção" alert, reconciliation every 5 min |
| LLM hallucinating or manipulated by *prompt injection* | Medium | Medium | Asymmetric authority, schema, required sources, limited weight and safe degradation |
| Changes in the Binance API | Medium | Medium | Own client with a small surface, following the changelog and *live* tests |
| LLM cost above the forecast | Low | Low | Daily cap, *prompt caching* and configurable models |
| Regulatory or location restriction | Low | High | VPS in an allowed region and a verified account |
| Tax obligations | Certain | Medium | Export of the trade journal (`fills`, costs, PnL) for the tax return. Consult an accountant |

## 6. Open decisions

1. **VPS:** provider and region.
2. **Production account:** dedicated account and initial capital.
3. **LLM weight:** depends on the "pure TA" × "TA + LLM" A/B, which still has to be implemented.
4. **Alert channel:** only Telegram, or e-mail too (through Healthchecks.io)?
5. **Observability in production:** keep Jaeger, Loki and SigNoz side by side, or keep just one (SigNoz is the heaviest)?

## 7. Possible improvements

- A "pure TA" clone of each profile for the A/B, and correlation between the analyst's sentiment and the return of the following days.
- Extra research triggers: a strong BTC move, a burst of news about an asset in the portfolio, or a stale reading before an entry.
- An automatic daily report on Telegram and a scheduled backup.
- A mean-reversion setup for a sideways regime.
- An ML *ranking* model (e.g. LightGBM) trained offline, as one more score.
- Multi-agent research with a *bull/bear* debate, only if the A/B shows a gain from the single analyst.
