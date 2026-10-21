# Risk, economics and validation implementation plan

Requested on 2026-10-19. Both observability stacks are intentionally under evaluation and
must remain available with their existing telemetry and dashboards. The consolidation
recommendation in the earlier assessment is deferred by the operator.

Work is reviewed through a pull request; no merge, production deployment or live orders
are part of this implementation. Live profitability and a future forward-test cannot be
certified by a software test run.

| Workstream | Implementation and acceptance |
|---|---|
| Entry safety | Serialize state transitions with final entry authorization; test pause, halt and flatten during research, between orders and with a send already in flight. |
| Position management | Run time/score exits and break-even before waiting for research; keep veto processing separate and re-read positions. |
| LLM authority | Approved: TA must qualify first; LLM can only veto or penalize. |
| Financial risk | Approved: opt-in cost-adjusted sizing, trailing-floor R:R and aggregate caps; active capital shares, thresholds and risk modes are preserved. |
| Research baseline | Explicit disabled mode with no opinions, exposure reduction or research calls; independent paper portfolios for A/B evaluation. |
| Economics | Auditable commission conversion, operating costs and research cost; distinguish trading and economic PnL and missing valuations. |
| Research budget | Approved: US$5/day, US$1 reserved per cycle, alerts for reduced/omitted research, uncertain charges retain the reservation. Token counting is not a billing guarantee. |
| Backtesting | Intrabar detail by default; explicit exploratory mode; reproducible manifests and stress assumptions; no new profitability claim without a completed run. |
| Portfolio | Approved: preserve USDT flatten; explicitly document remaining quote-asset exposure, defer conversion policy. |
| Performance | Shared closed-candle data and historical risk aggregates without the 1,000-position truncation. |
| Recovery | Approved: local encrypted backup and restore tests; external destination and heartbeat provider remain unconfigured. |
| Delivery | Ruff, formatting, mypy, unit and integration regression suites; targeted coverage and limitations recorded in the PR. Preserve both observability deployments. |

Validation must use fake exchange/API clients and isolated PostgreSQL containers. Never
load production credentials into the test environment. Changes to trading semantics must
be identified separately from corrections to implementation defects.
