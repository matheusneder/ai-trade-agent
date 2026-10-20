# AI Trade Agent assessment — 2026-10-19

> Historical initial assessment, translated into English during PR review. Subsequent
> operator decisions and implementation are in [hardening-plan.md](hardening-plan.md)
> and [hardening-validation.md](hardening-validation.md).
> The duplicate observability stacks are intentional: the operator deferred consolidation,
> and both stacks were preserved. Consolidation recommendations below are historical.

The project has a sound engineering foundation for a Binance Spot agent, but does not yet demonstrate sustainable net profitability in production. The best opportunity is to preserve the deterministic core, reduce operating costs and close the gaps between simulation and execution before adding more complex strategies.

The recommended objective is **net risk-adjusted return**, subject to explicit drawdown, exposure and cost limits. No stack or calibration guarantees profits.

**Scope and evidence.** This assessment covered decision, execution, risk, research, portfolio, configuration, lab, deployment and documentation. Historical results come from `doc/lab-results.md`; they were not recalculated for this assessment. Account balances, credentials, invoices and the operational database were not inspected, so the actual Demo state and infrastructure consumption were not established. The concurrency findings were static analysis, not reproduced incidents at this stage. Code references below use repository-relative paths and line numbers from the assessed version.

**Initial verification.** Python 3.14.3; `.venv/Scripts/python.exe -m pytest tests/unit -q`: **406 tests passed in 13.93 seconds**, outside the sandbox after access restrictions in the isolated environment. This initial assessment did not run integration/live/dashboard suites, a new backtest or coverage measurement. Passing existing tests does not invalidate the additional scenarios identified. At that stage only the report was added; application code and operational configuration were unchanged. Later implementation validation is recorded separately.

**Purpose and operation.** The agent selects liquid assets, calculates trend and breakout signals on 4h candles, consults an LLM market reading, sizes positions by profile and validates risk conditions. It sends FOK entries with native OPOCO/OCO protection, records intents and results in PostgreSQL and reconciles with Binance. Telegram is its operational interface; Freqtrade is its offline lab.

Configured capital is 1,000 USDT: `swing_trend` 65%, `momentum_alpha` 30% and `alt_atr` 5%. This is configuration, not proof of an actual balance. All three profiles are enabled.

**Preserve these choices.** Python/asyncio in a modular monolith; PostgreSQL as the transactional journal; Decimal for money and orders; Pydantic schemas; intents persisted before submission; identification and lookup of orders with uncertain outcomes; exchange-side protection; reconciliation; single-instance locking; and signals shared with the lab. These fit the operating volume and frequency. Redis, Kafka, microservices and Kubernetes have no demonstrated benefit in this scope.

The official documentation describes OPOCO using the quantity received by the entry, with commission and filter rules applied to pending orders. This supports the architecture, but native protection neither guarantees the desired execution price nor eliminates failures during replacement. [Binance: OPO/OPOCO](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/opo.md).

**Priorities.** P0: correct before using real capital; P1: resolve before increasing capital; P2: optimize after measurement. Effort is relative, not a delivery estimate.

| Priority | Opportunity | Expected effect | Effort |
|---|---|---|---|
| P0 | Revalidate risk state at submission | Prevent entry after pause/halt/flatten | Medium |
| P0 | Remove research waits from position management | Reduce exit/adjustment delays | Medium |
| P1 | Complete economic PnL and research budget control | Reveal and reduce cost-driven losses | Medium |
| P1 | One optional observability stack (subsequently deferred) | Less RAM, disk and maintenance | Medium |
| P1 | Truly isolated TA-only baseline | Measure the LLM's net contribution | Medium |
| P1 | Correct LLM authority semantics | Align behavior and the safety contract | Small/medium |
| P1 | Conservative backtests and frozen forward validation | Reduce overfitting risk | High |
| P1 | Aggregate risk, costs and realistic exits | Bring nominal and economic risk closer | Medium |
| P1 | Restorable backups and external heartbeat | Reduce outage impact | Small/medium |
| P2 | Shared data, aggregate queries and reproducible deployment | Less repeated work and hidden change | Medium |

**1. Risk state can become stale before buying — P0.**

In `src/trade_agent/decision/engine.py:326`, `run_profile()` reads risk state once. It then gathers signals, waits for research and processes exits; that same state reaches `_entries()` and `PreTradeContext` (`:344`, `:362`, `:504`). Neither `PositionService.open_position()` nor the gateway consults RiskGuard.

If `/halt`, `/pause` or a circuit breaker acts during research, the cycle can remain authorized by the old state. The existing lock serializes entries between profiles but does not coordinate them with risk-state changes.

Revalidate inside the entry section and coordinate final authorization with pause/halt/flatten through state versioning or a shared authorization lock. Recheck each order, available funds, data freshness and exposure. Acceptance: pause research, apply halt, release research and verify zero new entries; also halt between two orders. Explicitly define treatment of an order already in flight.

**2. Research delays position management — P0.**

`run_profile()` waits for `_reading()` before `_exits()`. Configuration allows 300 seconds per model call and research continuations, not a guaranteed total cycle deadline. Already armed stops/trailing orders remain on Binance, but time/score exits and break-even adjustments wait for research.

Run deterministic position management first. Consume the latest valid reading for veto and update research independently, with an overall timeout, explicit validity and missing-reading policy. Verify that LLM unavailability does not delay management or risk commands.

**3. Operating economics is the main obstacle — P1.**

`doc/04-status-and-go-live.md` estimates US$15–130/month for the LLM and US$20–40 for a VPS. These are historical project estimates, not current quotes or verified invoices. Together, US$35–170/month represents **3.5%–17% of configured capital**, assuming 1 USDT ≈ US$1, merely to cover these costs and before other expenses. Required return on invested capital may be higher because of reserves and allocation limits.

US$5/day nominally means US$150 over 30 days, or 15% of capital. Furthermore, `research/service.py:130` uses `budget_per_call=False`: spending is checked before a cycle, which may exceed the remaining balance. The configured amount is not a strict billing ceiling.

Track both trading PnL and economic PnL, subtracting LLM, infrastructure and other attributable expenses from the latter. Reserve per-cycle budget, constrain tokens/tools to available funds and reconcile estimates against actual billing. Set research spending according to demonstrated incremental contribution, not only tolerance for a daily bill.

**4. BNB commissions and incomplete nominal risk — P1.**

`execution/positions.py:186` calculates quote PnL but explicitly records commissions in other assets, such as BNB, separately. The risk monitor consumes that PnL. When third-asset commissions apply, profit and equity overstate complete economic results.

Convert each commission at the fill's contemporaneous price and retain the amount, conversion and quote source. Do not deduct fees already reflected in net quantity twice. `risk/monitor.py:41` also omits estimated liquidation costs from open PnL and returns zero without a book: distinguish a missing quote from absence of loss.

Sizing in `strategy/portfolio.py` uses stop distance without fees/slippage in estimated loss. Include them in the risk budget and test gaps, wider spreads and execution beyond the stop trigger.

**5. The documented A/B does not yet isolate the LLM — P1.**

`research/reading.py:35` preserves vetoes and exposure multipliers when `llm.weight = 0`. Without a reading, `ta_only_reduced` halves exposure. Neither zero weight nor removing the API key automatically produces a pure TA baseline.

Provide an explicit research-disabled mode: no calls, vetoes or LLM-caused reductions, while retaining common deterministic controls. Compare independent virtual portfolios for TA-only, TA with veto/reduction, and the existing hybrid. Share market snapshots and execution assumptions, but separate cash, positions and capital. Clones sharing one account interfere through the one-position-per-asset restriction.

Current `dry_run` records entry events; that alone is not a complete simulated portfolio with fills, fees, occupied capital and PnL. Implement this state for A/B evaluation. Measure net contribution, drawdown, exposure, turnover and vetoed opportunities, not only sentiment accuracy.

**6. The LLM can promote an entry below the technical threshold — P1.**

`strategy/portfolio.py:56` blends technical score and sentiment. The threshold at `:118` applies to that blend. For example, TA 0.60, weight 0.25 and sentiment/confidence 1 produce 0.70, above the current 0.67 minimum. Given a setup and the other filters, an entry that fails TA-only can pass.

The LLM cannot directly exceed the maximum exposure multiplier, but its authority extends beyond veto/reduction. This differs from the strict interpretation of the architectural promise. If strict asymmetry is intended, require technical eligibility first and allow only penalties, vetoes and size reductions. If the hybrid is intentional, document and evaluate that authority separately. Test that the chosen policy cannot create an ineligible entry.

**7. Infrastructure simplification can help immediately — P1 (subsequently deferred).**

`deploy/stack.yml` includes SigNoz and its services alongside Grafana, Loki, Alloy, Jaeger, the Docker proxy and metrics collection. The agent exports traces to two destinations. The core is lightweight; the full deployment combines two observability solutions.

| Layer | Initial proposal |
|---|---|
| Required core | Python agent + PostgreSQL |
| Minimum operation | Rotated JSON logs, Telegram, external heartbeat and backup |
| Optional dashboards | Grafana querying PostgreSQL |
| Optional diagnostics | One logs/traces solution enabled as needed |
| Research | Optional runtime LLM SDK with measured budget and benefit |
| Lab | Freqtrade outside the production host |

The initial preference was to retain existing Grafana SQL dashboards and remove mandatory SigNoz inclusion, with Loki/Alloy and Jaeger optional. If SigNoz's unified experience proved more valuable, choose that route and remove redundant destinations. Migrate alerts, essential metrics and investigation access before disabling components. The operator subsequently explicitly chose to keep both stacks during evaluation.

Measure RAM, CPU, disk/day and per-component cost over a comparable week; do not promise VPS capacity without that benchmark. Preserve SQLAlchemy, Alembic, pandas and TA-Lib until bottlenecks are demonstrated. Rewriting indicators or replacing PostgreSQL with SQLite creates divergence/migration risk without demonstrated benefit.

**8. The lab can still produce excessive confidence — P1.**

Project documentation shows optimized `swing_trend` dropping from +107.27% to −4.95% after adding intrabar detail. This compares documented methods, not the current fixed profile's return. It demonstrates sensitivity to execution assumptions.

Yet `lab/walk_forward.py:427` leaves `--detail` optional. Make 5m detail the default for acceptance evaluations and visibly mark exploratory runs without it. Use conservative ordering and sensitivity analysis for events still ambiguous within 5m. [Freqtrade: backtesting assumptions and detail timeframes](https://www.freqtrade.io/en/latest/backtesting/).

Add spread, slippage, FOK nonfill probability, post-close latency, minimum orders and portfolio constraints. The lab shares signals but not all execution, dynamic selection, global limits or research influence. Compare live-engine and simulator decisions on identical snapshots. Pin Freqtrade by version/digest rather than `stable`.

Keep currently unversioned `var/lab/` artifacts in durable storage: data/hashes, commit, configuration, image, orders and equity curve. Run lookahead analysis too; it helps detect future information but does not establish economic validity. [Freqtrade: lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

**9. Avoid overfitting and indirect selection through parameters — P1.**

`doc/lab-results.md` acknowledges reused holdouts, a static survivor universe and calibration by the median of training periods later than some evaluated windows. The `alt_atr` analysis involved 44 walk-forwards and selection using an already inspected period. Those selection results are exploratory.

The gain associated with 10.9% activation depends on falling just below minimum R:R when the stop reaches its cap. That embeds a volatility filter in another parameter. Express the filter directly if the hypothesis survives, and test stability across a broad range. Naming the profile for its fixed activation would also reduce ambiguity.

Freeze the strategy and start prospective evaluation with predefined acceptance and stopping rules. Four weeks of Demo help validate operation but may contain few trades and one regime; they are not statistical proof of profitability. Reconstruct historical universes using information available at each date, including newly listed and subsequently delisted assets. Declare limitations where suitable data is unavailable.

**10. Activation R:R is not captured return — P1.**

`risk/guard.py:196` uses `(activation − fees) / (stop + fees)`. With trailing, activation starts tracking; it is not the exit price. A 10.9% rise immediately followed by a 1.5% retracement yields approximately `(1.109 × 0.985 − 1) = 9.2365%` gross, before costs and execution. [Binance: trailing stop FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/trailing-stop-faq.md).

Separate trigger ratios from conservative gain/loss estimates and measure actual exit distributions. Changing this calculation changes `alt_atr` selection and requires new validation. Add aggregate position risk, concentration in correlated factors and joint-drop scenarios. One position per asset does not eliminate altcoin correlation.

Sizing uses fixed configured capital (`StrategyConfig.profile_capital`) while the monitor tracks gains and losses. Specify whether this is a fixed budget or an equity percentage. The latter needs available-capital/loss adjustments, a ceiling and explicit reinvestment rules; do not automatically increase risk to chase returns.

**11. Depeg needs its own policy — P1.**

`risk/monitor.py:59` estimates deviation through USDCUSDT and FDUSDUSDT. These are relative stablecoin references and do not independently identify which currency lost parity. `quote_depeg_pct` triggers flatten into USDT, reducing crypto exposure while preserving or increasing exposure to the affected quote asset.

Separate stopping entries, reducing crypto exposure and possible cash conversion. Conversion requires an explicit policy, independent references and operational validation; do not automate it from these two pairs alone.

**12. Maintenance and performance — P2.**

Share closed candles/features by symbol/timeframe/close across profiles; `_signals()` currently fetches sequentially per profile. Use an in-memory cache with validity and bounded concurrency respecting rate limits, without another service. Prioritize risk management and measure event-loop delay before moving computation to workers.

The monitor reads closed positions and recalculates aggregates for each snapshot; `Store.closed_positions()` caps them at 1,000. Move history metrics to period/profile SQL aggregates and correct loss streaks, avoiding silent truncation as data grows. Define retention for events, news and telemetry while preserving necessary fills/audit trails.

Automate lint, typing, unit and integration tests in CI. No `.github` directory was found, which does not rule out external CI. The declared 100% coverage target does not replace concurrency, data-failure or lab/live parity tests. Pin images and record experiment manifests; require explicit production passwords because Compose includes development defaults.

**13. Operational reliability before increasing capital — P1.**

The checklist still lists an external heartbeat, VPS, encrypted external backup and tested restoration as pending. Verify actual status because documentation mixes October updates with an October 3 status. Preserve runbooks and rehearse full recovery in isolation, including open positions and migrations. Monitor missing protection, reconciliation delay, stale data, budget, disk and database availability.

**Proposed sequence.**

1. Correct entry authorization and decouple position management from research. Test halt during research, between orders and during flatten.
2. Instrument economic PnL, commission conversion and per-decision costs. Measure infrastructure; observability consolidation was subsequently deferred by the operator.
3. Implement a pure TA baseline and independent virtual portfolios. Evaluate the LLM with comparable exposure/risk and attributed costs.
4. Align simulator/execution contracts, default to intrabar detail and add realistic costs/constraints.
5. Freeze configuration and evaluate a future period without recalibrating to its results. Measure net expectancy, uncertainty intervals, full-curve drawdown, turnover, exposure and regime performance.
6. Increase capital only when operational safety, recovery and net results support it. Compare cash and BTC buy-and-hold at similar exposure/risk. Reducing complexity or suspending the strategy is valid if no advantage emerges.

**Acceptance metrics.** No entries authorized by stale state; no lost management due to research timeout; reconciled costs; demonstrated restore; reproducible universe/configuration; incremental LLM value after cost; and positive results under plausible execution scenarios. A single window's worst drawdown is not whole-curve maximum drawdown. Define financial acceptance limits before observing the next test's results.

**Conclusion of the initial assessment.** The core stack is already appropriate. The most useful work is strengthening risk boundaries, measuring infrastructure, accounting for total costs and requiring prospective evidence for additional complexity. Verify that the system preserves capital when it must stop and that trading gains exceed operating costs. Observability consolidation remains deferred by the subsequent operator decision.
