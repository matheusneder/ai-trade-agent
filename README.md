# AI Trade Agent

Autonomous cryptocurrency trading agent for **Binance Spot**. It decides based on technical analysis and market research (LLM) and protects every position with native Binance orders (OPOCO/OCO with trailing). The protection keeps working even with the agent turned off.

- Documentation: [`doc/`](doc/README.md) (architecture, operation and lab)
- Current status: *paper trading* in Demo Mode since 2026-09-29; what is missing for production is in [`doc/04-status-and-go-live.md`](doc/04-status-and-go-live.md)
- Language: code, comments and documentation are in English; the agent's interface for its operator (Telegram messages, Grafana and SigNoz dashboards, log messages, the analyst's prompts) is in Portuguese.

> ⚠️ Experimental software. It does not constitute investment advice. Use Testnet/Demo and, in production, only capital you accept losing.

## Requirements

- Python 3.12+ (developed on 3.14)
- [uv](https://docs.astral.sh/uv/) (`pip install --user uv`; this README's commands use `uv`, and `python -m uv` works too)
- Docker (integration tests with PostgreSQL and deployment)

## Getting started

```bash
uv sync                      # creates .venv and installs the dependencies (dev included)
cp .env.example .env         # fill it in with the Testnet/Demo keys
uv run pytest                # unit and integration tests (no external network)
uv run pytest -m live        # tests against Binance Testnet/Demo (need keys in the .env)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Binance keys (Testnet / Demo)

1. Generate an Ed25519 key pair locally and keep the private key **out of git** (the `secrets/` folder is ignored):

   ```bash
   mkdir -p secrets
   openssl genpkey -algorithm ed25519 -out secrets/binance-testnet-ed25519.pem
   openssl pkey -in secrets/binance-testnet-ed25519.pem -pubout
   ```

2. Register the **public key** at:
   - Spot Testnet: https://testnet.binance.vision (log in with GitHub → *Generate Ed25519 Key*)
   - Demo Mode: https://demo.binance.com → *API Management*
3. Fill in `TA_BINANCE_API_KEY` and `TA_BINANCE_PRIVATE_KEY_PATH` in the `.env`.
4. To allow sending orders, set `TA_TRADING_ENABLED=true`. Without it, the client blocks every order.

Never enable the withdrawal permission on the API keys.

## OPOCO spike

Validates, in a new environment (Testnet or Demo), the order types the architecture depends on:

```bash
uv run python scripts/spike_opoco.py --env-file .env           # uses BTCUSDT by default
uv run python scripts/spike_opoco.py --env-file .env --symbol ETHUSDT
```

The script refuses the `prod` environment. The raw results go to `var/spike/`.

Result on the Spot Testnet (19/19, 2026-09-28) and findings about the API: [`doc/01-requirements-and-binance.md`](doc/01-requirements-and-binance.md) (§3).

## Manual operation CLI

Queries and protected orders (Testnet/Demo). Sending orders requires `TA_TRADING_ENABLED=true`; in production, also `--confirm-prod`.

```bash
uv run trade-agent info BTCUSDT
uv run trade-agent account
uv run trade-agent lists
# 20 USDT buy: OPOCO with a trailing TP (+3%, 1% pullback) and a fixed stop (-4%)
uv run trade-agent open BTCUSDT --quote 20 --tp-pct 3 --tp-trailing-bips 100 --stop-pct 4
# protect an existing balance with an OCO
uv run trade-agent protect BTCUSDT --qty 0.0003 --tp-pct 3 --tp-trailing-bips 100 --stop-pct 4
# close: cancels the protection and sells at market
uv run trade-agent close BTCUSDT --qty 0.0003 --list-id ta1-man-0a1b2c3d4e-0-L
```

## Running the agent

The agent needs PostgreSQL and the keys in the `.env`. At startup, it applies the migrations, syncs the clock and reconciles every position with Binance. After that, it:

- reconciles every 5 minutes and reacts to User Data Stream events;
- evaluates the stop conditions every minute (`config/stop_conditions.yaml`);
- collects news every 15 minutes;
- builds the universe and runs the decision cycle of each enabled profile at the candle close (the current profiles use 4h).

Use **Demo Mode** (`TA_BINANCE_ENV=demo`) to run the agent: the Spot Testnet has only ~20 days of history, and the universe ends up empty (see [`doc/runbook.md`](doc/runbook.md)). With `TA_TRADING_ENABLED=false` (the default), the agent runs in **simulation**: it decides and records the entries and exits as events, without sending orders. With Telegram configured (`TA_TELEGRAM_BOT_TOKEN` and `TA_TELEGRAM_CHAT_ID`), it sends alerts and accepts `/status`, `/pause`, `/resume`, `/halt` and `/flatten` (the latter with a confirmation code).

```bash
docker compose -f deploy/docker-compose.yml up -d postgres   # local database
uv run trade-agent run                                        # or: docker compose ... up -d agent
```

There can be only one active instance per database (*advisory lock*). Manual migrations with Alembic:

```bash
uv run alembic -x url=postgresql+asyncpg://trade_agent:trade_agent_dev@localhost:5432/trade_agent upgrade head
```

## Backtesting lab (Freqtrade through Docker)

Freqtrade's "shell" strategy imports the same signals package used in production (`trade_agent.signals`). The configuration and the parameters are generated from `config/profiles.yaml`.

```bash
# optimized walk-forward: hyperopt on the 12 months before each quarterly validation window
uv run python -m lab.walk_forward --profile swing_trend --optimize --download
# with the profile's current parameters, no optimization
uv run python -m lab.walk_forward --profile momentum_alpha
```

To compare another version of the code (A/B), point `TA_LAB_SRC` to that version's `src/` folder (e.g. a `git worktree`).

The windows go from 2023-01-01 to 2026-09-01 by default (`--start`, `--end`). The reports go to `var/lab/`, and the annotated summary of the calibration in use is in [`doc/lab-results.md`](doc/lab-results.md).

## Market analyst

Collects public news and metrics, triages them with `claude-sonnet-5` and produces a market reading (`MarketView`) with `claude-opus-5`. The safety rules are enforced by code: the LLM can only veto assets and reduce exposure. Models, sources, budget (US$ 5/day) and limits live in `config/research.yaml`; the key goes in the `.env` (`ANTHROPIC_API_KEY`).

```bash
uv run trade-agent research ingest                     # collects news and metrics (PostgreSQL)
uv run trade-agent research run --assets BTC,ETH,SOL   # one research cycle
uv run trade-agent research show                       # latest valid reading
uv run trade-agent research eval                       # evaluation with 31 labeled cases (~US$ 0.60)
```

## Observability

The agent records a telemetry snapshot every 5 minutes (`telemetry_snapshots`) and sends a heartbeat every minute to the `TA_HEALTHCHECK_URL` URL (e.g. Healthchecks.io). Grafana comes up with the compose, with 6 dashboards (overview, positions, performance, decisions and research, technical health and logs) and alerts on Telegram. Open it at `http://127.0.0.1:3000` (admin / `GRAFANA_ADMIN_PASSWORD`). The logs of every container go to Loki (collected by Grafana Alloy, kept for 30 days). The agent's traces (OpenTelemetry) go to Jaeger (7 days), with each component as a service, and show up in Grafana under *Explore* → *Traces*; logs and traces link both ways through the `trace_id`. The dependency graph is also in Grafana (Jaeger source, *Dependency graph*). Jaeger's own UI only opens on the host in development, with `DEV_JAEGER_UI=1` (runbook, §1.2). Alongside, SigNoz (`http://127.0.0.1:8080`; to reach it from another device on the local network in development, see runbook §1.3) gathers traces, logs and metrics: the agent's (equity, drawdown, risk state, LLM cost...) and each container's, with 4 dashboards (operation, technical health, LLM and containers) generated by `scripts/signoz_dashboards.py`. Incident procedures: [`doc/runbook.md`](doc/runbook.md).

```bash
docker compose -f deploy/docker-compose.yml up -d postgres grafana   # reads the root .env
uv run python -m scripts.grafana_dashboards   # regenerates the dashboards after editing the generator
```

## Layout

```text
src/trade_agent/      application code (see doc/03-architecture.md §4)
tests/unit/           unit tests
tests/integration/    integration tests (simulated Binance, Postgres in a container)
tests/live/           tests against Testnet/Demo (`live` marker)
scripts/              OPOCO spike and the Grafana and SigNoz dashboard generators
lab/                  backtesting lab (Freqtrade through Docker, walk-forward)
config/               allocation profiles (profiles.yaml) and analyst (research.yaml)
evals/                labeled cases to evaluate the LLM analyst
deploy/               docker-compose and provisioning
doc/                  architecture, plan and decisions
```
