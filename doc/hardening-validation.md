# Validation, economics and local recovery

Both observability stacks remain available. This change does not deploy the agent, merge
the PR, activate an external service or change the active allocation/risk parameters.
The historical assessment's recommendation to consolidate observability is deferred by
the operator.

## Entry and research behavior

Deterministic time/score exits and break-even run before research. Entries re-read risk
state after research and under final authorization before each send. A pause/halt waits
for an already authorized send to finish; it cannot recall an order already sent.
Authorization is process-local: run one trading agent per account/database. The durable
research reservation uses a PostgreSQL advisory lock and also covers manual research CLI
cycles sharing that database.

TA eligibility is mandatory. Positive LLM sentiment never promotes a candidate; negative
sentiment, veto and exposure reduction can restrict it. `llm.enabled: false` disables
decision-cycle research and uses neutral exposure. `weight: 0` alone does not disable veto.

The shipped research configuration preserves web research under US$5/day (UTC), with the
possible one-started-cycle overshoot accepted in D-033 and a 600-second cycle timeout.
Setting `budget.cycle_reserve_usd` explicitly opts into bounded RSS-only research. Token
estimates include headroom and can reduce output tokens. Provider web tools are omitted in
that opt-in mode, with INFO notifications; RSS sources and analysis remain. Errors (including a failed
triage followed by successful analysis) and retained reservations use HIGH. Explicit
4xx rejections other than 408 and transport-proven connection/pool failures before sending
release the reservation. Read/write failures, ambiguous connection errors, HTTP 408 and
server errors remain uncertain. Failed requests with uncertain billing
retain reservations across UTC days. This also covers requests crossing midnight.
At the next research cycle after 24 hours from creation, expired reservations are converted
atomically into full-value `llm_usage` estimates (`purpose=reservation_expired`,
`model=reservation:<token>`, zero tokens), attributed to their original UTC day, then removed.
Recovery also runs when bounded mode is disabled and after a process restart; it does not
depend on manual reconciliation. Only unexpired reservations count as outstanding. Legacy
reservations have no creation timestamp: their UTC day's end is used as a conservative
upper bound, so they expire at most 48 hours after that day's start. Retention and expiry
send HIGH notifications with reservation tokens. A hard-killed process cannot send a
retention notification; the next cycle still recovers its durable reservation and alerts
on expiry. Expiry is transactional; notifications are best effort after the transaction.

Automatic SDK retries are disabled. Message calls get at most two total attempts, only
for HTTP 429 or proven pre-send ConnectError/ConnectTimeout/PoolTimeout. `Retry-After`
seconds or HTTP dates are honored (one second fallback); the wait counts against the
cycle timeout. HTTP 408, all 5xx including 529, and read/write failures are not retried.

To correct an estimate using provider evidence, run against the intended database:

```bash
uv run python -m scripts.reconcile_research --token RESERVATION_TOKEN --settled-usd 0.25 --reason "Provider invoice/reference and explanation"
```

This is the verified total replacing the estimate only, excluding usage already recorded
for successful responses. A signed `reservation_adjustment` row is appended on the original
UTC day; `research.settlement.<token>` retains the estimate, expiry time, current settled
total and adjustment reasons/timestamps. Repeating the same total is idempotent. Never
delete a reservation or usage row to reconcile it. Full-value estimates may double-count
partially recorded cycles until corrected; this intentional conservative bias is visible
through the special purpose/model fields in the same usage table used by SQL cost dashboards.
Pricing configuration and token counts are estimates, not a provider invoice guarantee.
The implementation conservatively counts recorded usage plus outstanding reservations,
so concurrency can temporarily reserve more than the eventual expense.

## Opt-in risk experiments

`config/stop_conditions.yaml`, `pre_trade` accepts:

```yaml
risk_model: cost_adjusted
reward_model: trailing_floor
slippage_pct: 0.10
max_portfolio_risk_pct: 3
max_portfolio_exposure_pct: 80
```

These values illustrate an experiment, not recommended production settings. Active
defaults remain `nominal`, `activation`, zero slippage and no aggregate caps. Percentages
use percentage points. Cost-adjusted sizing includes round-trip fees and slippage in
risk. Trailing-floor reward uses activation followed by the trailing retracement, then
subtracts costs; gaps can still produce worse execution. Aggregate caps use managed
capital and conservative initial stop risk. No compounding or risk calibration is activated.

USDT depeg still flattens crypto positions into USDT. This reduces crypto exposure but
retains the USDT depeg risk. Conversion into another quote asset remains a separate decision.

## Reproducible simulation

```bash
uv run python -m lab.walk_forward --profile swing_trend --start 2026-08-01 --end 2026-09-01 --window-months 1 --pairs SOL/USDT ETH/USDT
uv run python -m lab.paper var/experiment.jsonl --slippage-bps 5
```

Walk-forward uses a pinned Freqtrade image and 5m detail by default, unique result folders
and manifests hashing source, configuration, lockfile and candle data. Aggregate caps are
rejected by this independent-profile simulator; use portfolio replay to test those caps.
Risk parameters are excluded from hyperopt.

Paper replay is an offline tool with independent cash and positions for `pure_ta` and
`ta_llm`. It accepts a chronological JSONL tape with timezone-aware `at` values:

- `{"kind":"quote","at":"2026-10-20T12:00:00Z","symbol":"SOLUSDT","bid":"100","ask":"100.01"}`
- A `decision` event has `profile` (configuration name), `candidates` (serialized
  `strategy.portfolio.Candidate` values), and `exposure_multiplier` as a decimal string.
  `lab.paper.CANDIDATES.dump_python(candidates, mode="json")` produces the candidate payload.

The tape must be collected independently of the selected strategy, including held assets,
with quotes frequent enough to evaluate stops. Automatic production tape collection is
not activated. The replay supports FOK entries, fixed stops and trailing take-profit only.
It models gap fills at observed bids, fees/slippage, decision exits and aggregate caps.
It does not reproduce queue depth, global circuit breakers, exchange rounding on every
protection replacement or continuous execution between quotes. Time exits without a fresh
quote use the last observed bid. Research and infrastructure costs must be deducted
separately. Sparse or synthetic tapes cannot establish profitability.

The short August 2026 smoke run completed with 4 trades and +9.35% before research/hosting
costs, versus +37.08% for its two-asset buy-and-hold basket. This is an execution smoke test,
not a new calibration or profitability finding. Freqtrade's reported closed-trade drawdown
was 0%; its wallet-based drawdown was 0.87%. Closed-trade drawdown alone understates
intratrade risk. Longer out-of-sample windows and prospective independent tapes are still
required before adopting experimental policies.

## Economic reporting

Third-asset commissions use the open of the fill's one-minute conversion candle, persisted
with source and time. This is a historical valuation proxy. Base/quote fees are already
included in cash-flow PnL and are not deducted twice. Missing conversion candles are
recorded as unresolved, with `position.fee_unpriced` events; reported PnL remains provisional.
Older closed positions are not silently rewritten.

```bash
uv run python -m scripts.economic_report --since 2026-10-01T00:00:00Z --until 2026-11-01T00:00:00Z --usd-in-quote 1 --costs var/operating-costs.json
```

The read-only report subtracts research usage and an explicit expense ledger over the
half-open interval `[since, until)`. Supply the USD-to-quote conversion explicitly.
Expense JSON: `[{"at":"2026-10-01T00:00:00Z","amount_usd":"10","description":"hosting"}]`.
`[]` means no expenses entered, not free operation. Check
`complete_commission_valuation` and `unpriced_commission_positions` before relying on totals.

## Backup and heartbeat

Set `TA_BACKUP_KEY` from a secret manager to a Fernet key; store the key separately from
archives. Use a container/database you explicitly intend to back up:

```bash
uv run python -m scripts.backup backup --container LOCAL_POSTGRES --database trade_agent --user postgres --archive var/trade-agent.backup.enc
uv run python -m scripts.backup restore-drill --container ISOLATED_POSTGRES --database audit_restore_test --user postgres --archive var/trade-agent.backup.enc
```

Create the empty restore database in an isolated test PostgreSQL beforehand. Restore
refuses nonempty targets and database names without `_restore_test`; it never drops a
database. Archives are authenticated/encrypted and cannot overwrite an existing file.
The implementation buffers dumps in memory and is intended for the current small database.
External upload, scheduling, retention and key recovery arrangements remain pending.
Existing heartbeat code and its local tests are retained; `TA_HEALTHCHECK_URL` remains
unset until the operator selects the external service.

## Review gates

Final review verification on 2026-10-21 (Python 3.14.3): **785 unit/integration tests
passed** in 128.49 seconds, with **100% statement and branch coverage** (5,705 statements,
1,114 branches). Ruff lint/format, mypy (194 files), and `git diff --check` passed.
Tests cover automatic expiry across UTC midnight, independent concurrent workers,
cancellation followed by a replacement service, conservative legacy-checkpoint expiry,
audited/idempotent adjustments and their CLI, selective retry success/exhaustion, HTTP
`Retry-After`, and cancellation during retry waits. A separate configuration assertion
confirms web is enabled and bounded mode is unset by default. Exchange/Claude calls are
simulated; PostgreSQL tests use an isolated container. The production checkout was untouched.

PR review follow-up verification on 2026-10-20 (Python 3.14.3): **769 unit/integration
tests passed** in 106.20 seconds, with **100% statement and branch coverage** (5,627
statements, 1,088 branches). Ruff, formatting, mypy (192 files), and `git diff --check`
passed. Added cases exercise explicit HTTP rejections, pre-send connection failures,
uncertain read/write failures, reservation reuse, and INFO versus HIGH notifications.
The changes were validated in an isolated worktree without restarting the running bot.

The subsequent review chose web research by default, opt-in bounded RSS-only research,
24-hour expiry with conservative estimates, selective retries, and focused commits in
the existing PR. Strict TA-first behavior was reconfirmed directly by the operator after
the review requested an uncapped default. D-037 records that clarification; D-038 retains
the prospective fee-accounting boundary. No production deployment is part of this review.

Initial implementation verification on 2026-10-20 (Python 3.14.3): **742 unit/integration tests passed**
in 97.83 seconds. The configured source coverage gate passed at **100% statements and
branches** (5,617 statements, 1,086 branches). Ruff, formatting, mypy (192 files) and
`git diff --check` also passed. Coverage does not establish market profitability.

```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest tests/unit tests/integration -q --cov=trade_agent
```

Tests use fake exchange/Claude clients and isolated PostgreSQL, including encrypted
backup/restore, concurrent reservations, pause/halt/flatten during research, authorization
with an in-flight order, strict TA eligibility, explicit TA-only mode, full risk history
past 1,000 trades, fee valuation and independent paper accounts. Dashboard E2E and live
exchange suites are separate; neither observability deployment is changed here.
