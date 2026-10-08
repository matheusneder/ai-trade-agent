# 01 — Requirements and the Binance API

> What the agent must guarantee and the Binance Spot API facts the design depends on. The API facts were checked on 2026-09-25 in the official documentation (`binance-spot-api-docs`) and on the public `exchangeInfo` endpoint. The 2026-09-28 spike confirmed them on the Spot Testnet, and the operation in Demo Mode has confirmed them since 2026-09-29 (§3). Links in [05-references.md](05-references.md).

## 1. Requirements

| # | Requirement | How the agent meets it |
|---|-------------|------------------------|
| R1 | Choose pairs and buy or sell without intervention | The universe is built every cycle, with liquidity, spread, history, delisting and *tier* filters. Then come the technical signals, the LLM analyst and each profile's portfolio, up to the OPOCO (doc 03, §6) |
| R2 | Allocation profiles | Declarative profiles in `config/profiles.yaml`, validated by schema. Today they are `swing_trend` and `momentum_alpha`, both on 4h (doc 03, §8) |
| R3 | Decisions with technical analysis, news and sentiment | Technical analysis is pure functions (`signals/`). News and sentiment go through the LLM analyst, with structured output capped by code (doc 03, §7) |
| R4 | Protection even with the agent turned off | Every entry is an OPOCO: Binance arms the trailing take-profit and the stop on its own server (§2; doc 03, §9) |
| R5 | Configurable stop conditions | `config/stop_conditions.yaml`, with the persisted states `running`, `paused`, `halted` and `flattening` (doc 03, §10) |
| R6 | Monitoring, alerts and dashboards | Telegram for alerts and commands, Grafana, Loki, Jaeger and SigNoz, and an optional external heartbeat (doc 03, §12) |
| R7 | Resilience and automatic recovery | The exchange is the source of truth. Intents are recorded before sending, IDs are deterministic, and reconciliation runs at startup and every 5 min (doc 03, §11) |
| R8 | Simplicity and mature components | The core is a Python process with PostgreSQL. Observability runs in separate containers, and the agent works without it (doc 03, §3) |

**Assumptions:**

- **Spot market without leverage**: there are only long positions, and "selling" means going back to the quote asset.
- **Swing trading** on 4h candles. It is not HFT or scalping: fees and LLM latency rule out high frequencies.
- Quote asset **USDT** (D-013).
- The agent trades up to `managed_capital` and ignores the balances that are not its own. Even so, a dedicated account is recommended.

## 2. What Binance offers

### 2.1 Native conditional orders (*order lists*)

The old `POST /api/v3/order/oco` endpoint is deprecated. The current one is `POST /api/v3/orderList/oco`.

| Type | REST endpoint | How it works | Use in the agent |
|------|---------------|--------------|------------------|
| **OCO** | `POST /api/v3/orderList/oco` | Two orders, one above and one below the price; when one fills, the other is canceled. `aboveType`: `STOP_LOSS_LIMIT`, `STOP_LOSS`, `LIMIT_MAKER`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`. `belowType`: `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`. Accepts `aboveTrailingDelta` and `belowTrailingDelta` | Re-protection and adjustment of an open position |
| **OTO** | `POST /api/v3/orderList/oto` | A working order (`LIMIT`/`LIMIT_MAKER`) that, once **fully** filled, triggers a pending order | Not used |
| **OTOCO** | `POST /api/v3/orderList/otoco` | An entry order that, once fully filled, places an **OCO pair** | Not used (OPOCO replaces it) |
| **OPO** | `POST /api/v3/orderList/opo` | Like OTO, but the pending order uses **the quantity actually received** in the entry, net of the fee | Not used |
| **OPOCO** | `POST /api/v3/orderList/opoco` | Like OTOCO, with the received quantity. **Only accepts a `BUY` entry and `SELL` pending orders** | **Every entry** |

Why OPOCO meets R4:

- A single request creates the entry **and** the protection. If the agent crashes right after sending the order, Binance places the TP/SL pair on its own as soon as the buy fills.
- The exit quantity is the one received, after the fee charged in the base asset. Binance snaps it to `LOT_SIZE`.
- It requires the `otoAllowed`, `opoAllowed` and `ocoAllowed` flags on the symbol, plus `allowTrailingStop` for the trailing. On 2026-09-25, 100% of the trading USDT pairs had all four. The universe drops the ones that do not (doc 03, §6.1).

### 2.2 Native trailing stop (protection that "moves" with the agent turned off)

- The `trailingDelta` parameter is expressed in **bips** (100 = 1%) and is accepted by `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT` and `TAKE_PROFIT_LIMIT`, including as OCO and OPOCO legs (`pendingAboveTrailingDelta`/`pendingBelowTrailingDelta`).
- On **sell** orders, the trigger is a drop of X bips from the **highest price** traded since tracking started.
- With a `stopPrice` given, tracking only starts when the price reaches the `stopPrice`. That creates the **trailing take-profit** the agent uses: a sell `TAKE_PROFIT` with `stopPrice` = +9.7% and `trailingDelta` = 100 only starts following the price after +9.7% and sells when the price pulls back 1% from the top.
- The `TRAILING_DELTA` filter caps the value: on BTCUSDT, between 10 and 2000 bips (0.1% to 20%). Since the limits vary by symbol, the agent validates each order against `exchangeInfo`.

### 2.3 Relevant limits and filters

BTCUSDT values on 2026-09-25. They vary by symbol, and the agent reads them from `exchangeInfo`.

| Filter or limit | Value | Impact |
|-----------------|-------|--------|
| `NOTIONAL.minNotional` | 5 USDT | Minimum order size |
| `MAX_NUM_ALGO_ORDERS` | 5 per symbol | `STOP_*`/`TAKE_PROFIT_*` orders count here. Each protected position uses 2. The agent opens at most one position per asset |
| `MAX_NUM_ORDER_LISTS` | 20 per symbol | Limit of open lists |
| `PERCENT_PRICE_BY_SIDE` | range per side | Limit prices too far away are rejected |
| Rate limits | `REQUEST_WEIGHT` 6000/min; `ORDERS` 100/10s and 200,000/day; `RAW_REQUESTS` 300,000/5min | Since 2026-04-02, successful order submissions have weight 0. Queries still weigh (the "Peso usado (1 min)" panel) |
| *Price Range Execution Rule* | range around a reference price | In extreme moves, a taker order may **expire** with `expiryReason = EXECUTION_RULE_PRICE_RANGE_EXCEEDED`, including a triggered stop. The position goes to `UNPROTECTED`, and the agent re-protects or sells (D-010) |

Other API points:

- **There is no atomic replacement of an *order list*.** `cancelReplace` only applies to simple orders, and `amend/keepPriority` only reduces quantity. To move the stop of an OCO, the agent cancels the list and creates another, with a sell *fail-safe* if the new OCO is rejected (D-007).
- **User Data Stream:** the `listenKey` model was deprecated in April 2025. The agent uses the **WebSocket API** (`userDataStream.subscribe.signature`) and reconnects proactively on the `serverShutdown` event.
- **Fees:** `GET /api/v3/account/commission` returns the account's fees. The agent measures the fee of each fill from the `fills`, and the pre-trade check uses the configured round-trip fee (`pre_trade.round_trip_fee_pct`, 0.2%).
- **Delistings:** `GET /sapi/v1/spot/delist-schedule` lists the pairs with a scheduled delisting, which the universe excludes. The route only exists in production: Testnet and Demo Mode do not have the `/sapi` routes (HTTP 404), and there the agent does not query it.

### 2.4 Environments

| Environment | URL | Use |
|-------------|-----|-----|
| **Spot Testnet** | `testnet.binance.vision` | Validate order types, errors and filters (spike and `pytest -m live`). Prices and liquidity are not realistic, and the history is short (about 20 days): the signals need 201 candles and the universe 30 days, so the decision cycle does not run there |
| **Demo Mode** | REST `demo-api.binance.com`, WS API `demo-ws-api.binance.com` | *Paper trading* with real market data and the same API. It is where the agent runs today (`TA_BINANCE_ENV=demo`) |
| **Production** | `api.binance.com` | Not used yet. Orders require `--confirm-prod` in the CLI (doc 04) |

### 2.5 Account security and eligibility

- **API key:** **Ed25519** (Binance's recommendation), only with *Reading* and *Spot Trading*, **withdrawals disabled** and the IP list restricted to the server's fixed IP.
- **Restricted location:** Binance answers **HTTP 451** to IPs from restricted locations (e.g. the US and Ontario), which includes cloud regions in those countries. The server must be in an allowed region (e.g. São Paulo, Frankfurt, Tokyo).
- **Sub-accounts:** only for corporate accounts or **VIP 1+** individuals. That is why the profiles share the same account: each one has a share of `managed_capital`, and the positions are identified by the prefix of the order IDs (doc 03, §8).

## 3. Confirmed behaviors

**Spot Testnet, 2026-09-28** (`scripts/spike_opoco.py`, BTCUSDT): 19 out of 19 checks passed. Tested: OPOCO with a `LIMIT FOK` buy, `TAKE_PROFIT` with activation and trailing and a fixed `STOP_LOSS`, the variant with a trailing-only `STOP_LOSS`, the non-executable FOK (entry `EXPIRED`, legs not armed), the standalone OCO with lookup and cancellation, and the User Data Stream over the WebSocket API. The *live* test (open, arm, adjust and close) also passed on Testnet.

**Demo Mode, since 2026-09-29:** the agent opens and protects real *paper trading* positions with OPOCO. The protected quantity is the one received after the fee, rounded down to `LOT_SIZE` (e.g. 25.10487 received and 25.104 protected). That confirms the OPO semantics with the fee charged in the base asset.

Findings the code handles:

1. **Pending legs without `origQty`:** in the `orderList/opoco` response, the TP and the SL come back `PENDING_NEW` and without a quantity, which is only set when the entry fills. On pending legs, a missing `origQty` counts as 0.
2. **The legs stay `PENDING_NEW` for a few moments** after the entry fills. The sync recognizes that interval (verdict `ARMING`, "OCO being armed") and queries again.
3. **The `listClientOrderId` can be reused after the list finishes.** That is why the `seq` of the IDs always moves forward, and an uncertain outcome leads to a lookup, never to a resend (D-006, D-012).
4. **`contingencyType` comes back as `"OTO"` on an OPOCO list.** It is informational only: no logic depends on that field.
5. **The received balance stays locked by the OCO** (`locked`, not `free`). What was received is measured from the fills, not from the free balance.

`scripts/spike_opoco.py` repeats these checks in a new environment and refuses the `prod` environment (README).

## 4. Data sources

| Source | Type | Use in the agent |
|--------|------|------------------|
| Binance Spot (klines, 24h tickers, best bid/ask) | Technical | Signals, universe (volume, spread) and entry price |
| Binance `delist-schedule` | Events | Exclusion from the universe (production only) |
| Binance website announcements (listings, delistings, news) | Events | News for the analyst |
| RSS: CoinDesk, Cointelegraph, The Block, Decrypt | News | News for the analyst |
| Fear & Greed Index (alternative.me) | Sentiment | Metric for the analyst and the `fear_greed_below` circuit breaker |
| Binance Futures, public endpoints (*funding rate*, *open interest*) | Quantitative sentiment | Metrics for the analyst (up to 12 symbols) |
| Claude's web search and fetch (`web_search`, `web_fetch`) | On-demand news | The analyst's verification step, with up to 5 searches and 3 fetches per cycle (US$ 10 per 1,000 searches, plus tokens) |

The sources live in `config/research.yaml` (D-020), all public and keyless. Each one fails in isolation.

## 5. Known pitfalls and how the agent avoids them

| Pitfall | Consequence | Mitigation |
|---------|-------------|------------|
| LLM deciding and executing orders | Non-determinism, hallucinated price or quantity, high cost | The LLM **only produces structured data** (scores and vetoes), and the code is what executes. It **can reduce or veto risk, never increase it** |
| *Prompt injection* through news | Malicious text manipulates the decision | External content goes in as untrusted data. The output is validated by schema and capped by code, and the LLM has no write or execution tools |
| Backtesting with an LLM (*look-ahead*) | The model "knows the future" of past dates | The lab tests only the technical part. The LLM's value has to be measured on Demo (the A/B does not exist yet; doc 04) |
| Ignored fees | A strategy profitable on paper and losing in practice | The lab charges 0.1% per side. The pre-trade check requires a minimum R:R after the round-trip fee, and the PnL comes from the real fills (`fills`) |
| Partial fill of the entry | Part of the position stays unprotected, because the OPOCO only arms after the full fill | `LIMIT` entry with `FOK` in both profiles. The `limit_maker_gtc` mode handles the partial fill (doc 03, §9.3) |
| Stop-limit "skipped" in a *gap* | The stop triggers but does not fill | Lower leg `STOP_LOSS` (at market), with the `expiryReason` monitored |
| Delisting or *depeg* | Abrupt loss or a stuck asset | The universe excludes scheduled delistings, the analyst can veto, and the `quote_depeg_pct` circuit breaker sells everything if USDT loses its peg |
| Out-of-sync clock | Rejection by `timestamp` and `recvWindow` (`-1021`) | Sync at startup and every 10 min, with three samples, and a new measurement with one retry when Binance refuses (runbook, §2.6) |
| Two instances running | Duplicate orders | Exclusive *advisory lock* in the database and deterministic order IDs |
