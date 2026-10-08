# AI Trade Agent — Documentation

Autonomous cryptocurrency trading agent for **Binance Spot**. It decides with technical analysis and with the market reading of an LLM analyst (news and sentiment), runs configurable risk profiles and protects every position on the exchange itself. It also recovers on its own after a crash.

## In short

- **Protection on Binance:** every position is born as an **OPOCO**, a buy that, once filled, arms an OCO with a **trailing take-profit** and a **stop-loss** on Binance's server. The position stays protected and keeps taking profit with the agent turned off.
- **Recovery:** with what is critical kept on the exchange, a crash is resolved at the next startup: the agent **reconciles and moves on**.
- **The LLM is an analyst, not an operator:** it produces a structured reading, with asymmetric authority. It can veto or reduce risk, never increase it.
- **Pieces:** a Python process with PostgreSQL. Alongside it are Telegram (alerts and commands), Grafana, Loki, Jaeger and SigNoz (observability), Freqtrade as an offline backtesting lab and Binance's Demo Mode for *paper trading*.
- **Status:** *paper trading* on Demo since 2026-09-29, with the `swing_trend` and `momentum_alpha` profiles (4h). The *go-live* checklist is in doc 04.

## Documents

| # | Document | Contents |
|---|----------|----------|
| 01 | [Requirements and the Binance API](01-requirements-and-binance.md) | Requirements, native *order lists* and trailing, limits, environments, account security, behaviors confirmed on Testnet and Demo, data sources and pitfalls |
| 03 | [Architecture](03-architecture.md) | Principles, containers, stack, code, tasks, decision cycle, LLM analyst, profiles, OPOCO and the position lifecycle, risk, persistence and recovery, monitoring, security, deployment and the decision log (D-001 to D-031) |
| 04 | [Current status and *go-live*](04-status-and-go-live.md) | What has been validated, production checklist, testing strategy, costs, risks and open decisions |
| 05 | [References](05-references.md) | Documentation of Binance, the tools and the data sources |
| — | [Runbook](runbook.md) | Operation: start and check, logs, traces, SigNoz, incidents, Telegram commands and maintenance |
| — | [Lab results](lab-results.md) | The *walk-forward* that calibrated the profiles in use |

## Disclaimer

The numeric parameters (risk percentages, stops, weights) are the project's configuration and **do not constitute investment advice**. Trading crypto assets involves the risk of losing all the capital. Validate everything in backtests and *paper trading* and only use capital you accept losing.
