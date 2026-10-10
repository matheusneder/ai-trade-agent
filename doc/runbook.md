# Operations runbook

> Procedures for whoever operates the agent. The current status and the production checklist are in [doc 04](04-status-and-go-live.md). **Golden rule:** the protections (OCO) live on Binance and keep working with the agent stopped. When in doubt, prefer `/halt` (keeps the protections) over `/flatten` (sells everything).
>
> The replies to the Telegram commands are in English. The rest of the agent's interface is in Portuguese: alerts, dashboard and panel names and alert titles are quoted here as they appear on screen.

## 1. Start and check

```bash
docker compose -f deploy/docker-compose.yml up -d     # the whole stack (agent and observability)
docker compose -f deploy/docker-compose.yml logs -f agent
```

| Check | Where | Expected |
|-------|-------|----------|
| Agent started | `agent.started` log / Telegram `/status` | reconciliation without orphans; state `running` |
| Mode | `/status` (1st line) | "SIMULATION" while `TA_TRADING_ENABLED=false` |
| Telemetry | Grafana → *Saúde técnica* → "Segundos desde a última foto" | below 360 s |
| Heartbeat | Healthchecks.io dashboard (only with `TA_HEALTHCHECK_URL` set) | a ping every minute |
| Logs | Grafana → *Logs* | lines arriving from every service, no tracebacks |
| Traces | Grafana → *Explore* → *Traces* (Jaeger source) | traces arriving from each agent component |
| Ports | the check below, from the host | Grafana (302), SigNoz (200) and SigNoz's OTLP (404) answer |
| SigNoz | `http://127.0.0.1:8080` → *Services* and *Logs* | agent components, logs of every service and `trade_agent.*` metrics |

After starting or recreating the stack, check the ports from the host. `000` is a port with no answer: if the service is up, see the note about ports in §1.2.

```bash
for p in 3000 8080 14318; do curl -s -o /dev/null -m 5 -w "$p: %{http_code}\n" http://127.0.0.1:$p/; done
```

**Grafana and SigNoz from another device on the local network, only in development** (e.g. a phone on the same Wi-Fi): put `DEV_GRAFANA_UI_HOST=0.0.0.0` and/or `DEV_SIGNOZ_UI_HOST=0.0.0.0` (or the machine's LAN IP) in the `.env`, run `docker compose -f deploy/docker-compose.yml up -d` and open `http://<machine's LAN IP>:3000` (Grafana) or `:8080` (SigNoz); on Windows, `ipconfig` shows the IP. Only these UIs open: SigNoz's OTLP (14318), PostgreSQL and the other ports stay on `127.0.0.1`. The login is still required (in Grafana, admin / `GRAFANA_ADMIN_PASSWORD`, so never leave the default `admin`), but over plain HTTP: use it only on a trusted network. On Windows, the Rancher Desktop installation already lets `host-switch.exe` through the firewall (inbound, every profile), so with a variable on its UI answers on any network the machine joins. To close them, delete the lines and run the same command. Changing these variables also restarts the agent, which receives the whole `.env`: do it outside a decision cycle. On a VPS, leave them unset.

**Environment:** the **Spot Testnet** is for validating orders (spike and `pytest -m live`), not for the decision cycle. It is reset periodically and has only ~20 days of history (the signals need 201 candles and the universe, 30 days), besides artificial volumes. The universe ends up empty and the log shows `decision.empty_universe`. For *paper trading*, use **Demo Mode** (`TA_BINANCE_ENV=demo`, with keys created at demo.binance.com), which uses real market data.

The periodic tasks (risk, telemetry, news and heartbeat) run right at startup and then at every interval. The first decision cycle happens at the next 4h candle close (00, 04, 08, 12, 16 and 20 UTC).

Grafana listens only on `127.0.0.1:3000`. On a VPS, use a tunnel: `ssh -L 3000:127.0.0.1:3000 user@vps`.

### 1.1 Logs and the DEBUG level

`TA_LOG_LEVEL=DEBUG` in the `.env` (then `docker compose ... up -d agent`) shows the detail of each step. `INFO` goes back to the summary. The JSON logs carry an `event` field you can filter on:

| Event | What it shows |
|-------|---------------|
| `rest.request` / `rest.request_failed` | method, path (without the *query*), status, latency, used weight |
| `risk.snapshot` / `risk.evaluated` / `risk.hit_already_fired` / `risk.hit_unchanged` / `risk.trigger_rearmed` | equity, day open, peak, BTC 1h, peg, API errors; triggers met, the ones that already acted (D-026), the ones held back by a more restrictive state and the rearmed ones |
| `decision.cycle_start` / `decision.signal` / `decision.reading` / `decision.plan` / `decision.exit_check` / `decision.pre_trade_rejected` | eligible universe, score and setup per asset, the analyst's reading, ideas and rejections, exit check |
| `position.sync` / `order.*` / `order_list.*` / `protection.replace` | the verdict of each sync and the path of each order |
| `reconcile.done` / `reconcile.position` / `reconcile.intent` | summary and detail of each reconciliation |
| `llm.request` / `llm.response` / `analyst.*` / `research.*` | model, tokens, cost and regime (never the content); collected sources and news |
| `runtime.job` / `runtime.task_failed` / `schedule.next_run` | duration of each task, failures with the task name, next decision cycle |
| `telegram.*` / `alert.sent` / `heartbeat.ok` / `telemetry.recorded` | Telegram commands and calls, alerts, heartbeat and telemetry |

```bash
docker compose -f deploy/docker-compose.yml logs -f agent | grep -E '"event": "(risk|decision)\.'
```

Secrets never go to the logs: keys, signatures, passwords and tokens are masked (`***`) and the HTTP libraries (which log URLs with tokens) stay at `WARNING`.

**In Grafana (Loki):** Alloy sends the logs of every project container to Loki, where they are kept for 30 days, even after a `docker compose down`. The *Logs* dashboard filters by service, level and text. In *Explore* (*Logs* source), the labels are `service` and `level`, and the agent's `event` can be filtered without parsing the JSON:

```logql
{service="agent", level=~"error|critical"}                          # agent errors and tracebacks
{service="agent"} | event="risk.state_changed"                       # risk state changes
{service="agent"} | json | event="decision.cycle" | profile="momentum_alpha"
sum by (event) (count_over_time({service="agent"} | event!="" [1h]))  # most frequent events
```

A Python traceback arrives as a single entry, with `level="error"`. In PostgreSQL, `FATAL: terminating connection due to administrator command` on a restart is expected. The Alloy UI (`http://127.0.0.1:12345`) shows the discovered containers and the pipeline's health. On Windows with Rancher Desktop, it does not open (see the note about ports in §1.2).

### 1.2 Traces (OpenTelemetry and Jaeger)

Every agent task becomes a trace in Jaeger (7 days on disk): the risk check, the news collection, the reconciliation, each decision cycle, each Telegram command and the startup. Each component shows up as a service (`trade-agent.runtime`, `.risk`, `.decision`, `.research`, `.llm`, `.execution`, `.exchange`, `.reconcile`, `.db`, `.telegram`, `.telemetry`).

Jaeger publishes no ports on the host: Grafana and the agent reach it on the internal network, and every published port is one more NAT rule Rancher Desktop may leave stale (note below). The graph of who calls whom is also in Grafana (table below).

**Jaeger UI, only in development on Windows** (`http://127.0.0.1:16686`, with the **System Architecture** tab): the `jaeger-ui` relay comes turned off. To turn it on, put `DEV_JAEGER_UI=1` in the `.env` and run `docker compose -f deploy/docker-compose.yml up -d`. To turn it off, delete the line and run the same command. On Linux or Kubernetes, leave it off. The relay looks Jaeger up by name on every connection: recreating Jaeger does not bring the port down. Changing the variable also restarts the agent, which receives the whole `.env`: do it outside a decision cycle.

| Where | What for |
|-------|----------|
| Grafana → *Explore* → *Traces* → *Search* (service `trade-agent.runtime`, operation `job ...`) | the whole task, step by step, with the duration of each call; on each span, **Logs for this span** opens the logs in Loki |
| Grafana → *Explore* → *Traces* → *Search* (tag `error=true`) | failed traces: exception and message on the span |
| Grafana → *Explore* → *Logs* | an agent log with a `trace_id` shows **Abrir trace** (open trace) |
| Grafana → *Explore* → Jaeger source → *Dependency graph* | who calls whom among the components: the same graph as Jaeger's *System Architecture* tab |

**What each span records:** Binance (method, path without the query, status, used weight), SQL (statement with `$1`, `$2`..., never the values), LLM (model, purpose, tokens, cost and stop reason, never the content), Telegram (method and command, never the token or the text), decision (profile, evaluated, entries, exits and rejections), risk (equity, triggers and state changes). The heartbeat and the wait for Telegram messages produce no traces.

Jaeger is pinned at 2.20: 2.21 removed the v1 API that Grafana's datasource uses (a test fails if the version changes). With Jaeger down, the agent goes on normally and drops the spans.

**A port does not open on Windows, but the service is up?** With Rancher Desktop, the connection opens and the response never arrives. To confirm, test inside the VM: `rdctl shell -- wget -qO- http://127.0.0.1:8080/` answers. Rancher Desktop carries Windows traffic to the container with its own NAT rules (one per container IP, in the `nat` table, `DOCKER` chain), and there are three cases where the path fails:

- **Stale rule in front:** when a container stops, Rancher Desktop cannot delete its rule, because it no longer knows the IP. `rancher-desktop-guestagent.log` shows `--delete DOCKER ... --to-destination :16686` with `Bad rule`. If the container comes back with another IP, the old rule comes first and the port stops answering. That happened to Jaeger's 16686 and, after recreating the whole stack, to 4318 too. Every recreation of the stack can leave stale rules, for any port: to see all the rules of a port, run `rdctl shell -- sudo iptables -t nat -S DOCKER | grep "dport 4318 "`. Since then, Jaeger publishes no ports, and 16686 only opens through the development relay (above), which stays up when Jaeger is recreated. Recreating the container does not fix it. Restart Rancher Desktop (`rdctl shutdown` and open it again), which clears the rules. The containers come back on their own (`restart: unless-stopped`).
- **Missing Docker accept rule:** for each published port, Docker creates a rule that lets outside traffic reach the container. It has gone missing before, after recreating the stack and the machine waking up from sleep: SigNoz's 8080 stopped, with the right NAT rule. To confirm, run `rdctl shell -- sh -c 'nft list ruleset | grep "dport 8080 .*accept"'`; if nothing comes out, the rule is missing (`host-switch.log` shows `error dialing "192.168.127.2:8080": context deadline exceeded`). `docker restart <container>` makes Docker recreate the port's rules.
- **Container on several networks (Alloy, port 12345):** Rancher Desktop creates a rule for each network, and the first one wins. Docker only accepts the published port through the network it chose for that, and traffic coming in through the others is dropped. Restarting does not fix it. The Alloy UI is unreachable on Windows, but works on a Linux box with regular Docker, like the VPS. To check Alloy on Windows, use `rdctl shell -- wget -qO- http://127.0.0.1:12345/-/ready` (inside the VM) or the `{service="alloy"}` logs in Grafana.

That is why the project publishes only the ports used day to day (Grafana, SigNoz, SigNoz's OTLP, Alloy and Postgres), and the Jaeger UI only through the development relay (above). After recreating the stack, the port check in §1 shows right away whether a rule went stale.

### 1.3 SigNoz (traces, logs and metrics in one place)

SigNoz runs alongside Jaeger and Loki, for comparison (`http://127.0.0.1:8080`). On the first access, it asks you to create the admin account. It receives:

| Signal | From | What you can see |
|--------|------|------------------|
| Traces | the agent, with the same spans as Jaeger | *Services*: latency, throughput and errors per component and operation, computed from the spans; *Service Map*; LLM cost per model (from the tokens in the spans) |
| Logs | Alloy, with an OTLP copy of everything that goes to Loki | search by service, level and any field of the agent's JSON (`event`, `profile`, `symbol`...); each log with a `trace_id` opens the trace |
| Agent metrics | the agent, every minute | `trade_agent.equity`, `.drawdown`, `.pnl.*` (`.pnl.total` per scope: `global` and each profile, realized + open), `.exposure`, `.positions.active`, `.binance.error_rate`, `.binance.weight_used_1m`, `.clock.offset`, `.risk.state` (0 running, 1 paused, 2 halted, 3 flattening); counters `.llm.cost`, `.llm.tokens`, `.decision.cycles`, `.decision.entries`, `.decision.exits`, `.risk.state_changes` |
| Container metrics | `container-metrics` (OpenTelemetry Collector, `docker_stats` through the read-only proxy) | CPU, memory, network and disk of each project container |

SigNoz's internal logs (ClickHouse, keeper, migrations) go neither to Loki nor to SigNoz: see them with `docker compose ... logs <service>`. The agent sends traces to both destinations with separate queues: one being down does not affect the other.

**From another device on the local network** (a phone on the same Wi-Fi, only in development): `DEV_SIGNOZ_UI_HOST`, see §1.

**Dashboards** (under *Dashboards*, with the `projeto: trade-agent` tag):

| Dashboard | What for |
|-----------|----------|
| Trade Agent · Operação (operation) | equity, the day's result, drawdown, exposure, positions and the worst risk state right now; equity history (with the day open and the peak) and result; gains and losses per scope (realized + open, since the start); risk state per scope; cycles, entries and exits per profile; the latest decision cycles |
| Trade Agent · Saúde técnica (technical health) | Binance failures and weight, clock offset, errors in the logs and failed tasks; p95 latency per Binance endpoint, per task and per database operation; error responses by status; spans per component; warnings and errors per service and the agent's latest ones |
| Trade Agent · LLM | cost, tokens, calls and latency in the period; cost per model and purpose, tokens per direction, p50/p95 latency and the latest calls |
| Trade Agent · Contêineres (containers) | CPU, memory, network and disk per compose service; memory relative to the limit; log lines per service |

The dashboards are code: the `scripts/signoz_dashboards.py` generator writes the JSON to `deploy/signoz/dashboards/` and applies them through the API. A test fails if the JSON files get out of date. Edit the generator, not the dashboard in the UI: applying again overwrites, by `name`, whatever was changed by hand. Applying requires the key of a service account with the *Editor* role (*Settings → Service Accounts*) in `TA_SIGNOZ_API_KEY` in the `.env`:

```bash
uv run python -m scripts.signoz_dashboards --apply   # creates or updates the four dashboards
uv run python -m scripts.signoz_dashboards --check   # runs every query over the last 24 h
```

In `--check`, an empty panel may just be a lack of events (no state change, no failure); an `ERRO` (error) means an invalid query.

**Deployment:** the manifests come from Foundry, SigNoz's official tool, from `deploy/signoz/casting.yaml` (pinned versions). To update the version, edit the casting and regenerate; a test fails if `pours/` gets out of date:

```bash
docker run --rm -v "$PWD/deploy/signoz:/work" -w /work signoz/foundryctl:v0.3.0 forge --no-ledger --no-updater
```

The local adjustments (ports only on `127.0.0.1`, OTLP on 14318, leaving 4318 free, log rotation) live in `deploy/signoz/compose.override.yaml`. At startup, a helper container downloads `histogram-quantile` from SigNoz's official GitHub releases (a ClickHouse function), as in the official deployment. SigNoz uses more memory than the rest of the stack (ClickHouse): on a VPS, set aside at least 4 GB for it.

## 2. Incidents

### 2.1 Agent down ("Agente sem telemetria" alert or Healthchecks.io)

1. The positions stay protected by the OCOs on Binance. There is no rush to sell.
2. `docker compose ... ps` and `logs --tail 200 agent`. Look for `runtime.task_failed`, `AlreadyRunningError` and database errors. If the container was removed, the logs from before the crash are still in the *Logs* dashboard. In Grafana → *Explore* → *Traces*, the search with `error=true` shows at which step the task failed.
3. Restart with `docker compose ... restart agent`. The startup applies the migrations, syncs the clock and **reconciles** everything: pending intents, positions and orphans.
4. Check `/status` and the *Posições* dashboard.

### 2.2 Unprotected position (critical alert)

The agent tries to re-protect right away. If the new OCO is rejected, it sells at market (*fail-safe*, D-007). If the alert persists:

1. See the event in the *Saúde técnica* dashboard → "Eventos altos e críticos" (high and critical events; e.g. `EXECUTION_RULE_PRICE_RANGE_EXCEEDED`, filter, balance).
2. `/halt <profile>` to block new entries while you investigate.
3. Protect it manually with `uv run trade-agent protect SYMBOL --qty ... --tp-pct ... --tp-trailing-bips ... --stop-pct ...` or close it with `trade-agent close`.

### 2.3 Circuit breaker tripped (`paused`, `halted`)

| State | What it means | What to do |
|-------|---------------|------------|
| `paused` with a deadline | automatic pause (e.g. a BTC drop, Fear & Greed, API errors) | nothing; it returns on its own at the end of the *cooldown* |
| `paused` without a deadline | reconciliation mismatch (an orphan, or an error in two reconciliations in a row) or `/pause` | diagnose and `/resume <scope>` |
| `halted` | drawdown from the peak, target reached or `/halt` | analyze the cause in the *Visão geral* dashboard and `/resume` when it is safe |
| `halted` after a flatten | *depeg* of the quote asset or `/flatten` | confirm the sells went through (*Posições* dashboard) before the `/resume` |

Each trigger alerts and acts once per occurrence (D-026). While the condition still holds (e.g. the 4 consecutive losses, which only reset with a winning trade), `/resume` and the end of the *cooldown* hold. The `/resume` reply lists those triggers. They only act again if the condition clears and comes back, or if it gets one full limit worse (4 → 8 consecutive losses; daily loss of 3% → 6%; drawdown of 15% → 30%). A trigger that occurred during a more restrictive state (e.g. a `/pause` without a deadline) acts on the first `/resume`: repeat the `/resume` if you want to go on anyway.

### 2.4 Banned IP (HTTP 418) or too many requests (429)

The errors carry the `Retry-After` reported by Binance, and an infrastructure failure rate above 20% in 5 min pauses entries for 30 min. On a 418, stop the agent (`/halt` and `docker compose ... stop agent`) until the ban ends. Review the "Peso usado (1 min)" (used weight) panel and lower the frequency of tasks if needed.

### 2.5 LLM analyst unavailable or out of budget

Each profile follows its `llm.on_failure` (`ta_only`, `ta_only_reduced` or `pause_entries`). See the *Decisões e pesquisa* dashboard ("Ciclos do analista por status", "Custo diário do LLM"). The daily cap lives in `config/research.yaml` (`budget.daily_usd`). It is checked once per research cycle (D-033): a cycle that started runs to the end, so the day may close slightly above the cap (by at most one cycle, about US$ 0.50). The profiles of the same timeframe share one research per close (D-032), so its `trigger` names both (`ciclo:swing_trend+momentum_alpha`).

### 2.6 Out-of-sync clock (`-1021 Timestamp outside recvWindow`)

The agent measures the offset between the local clock and Binance's at startup and every 10 minutes, and compensates for it in signed requests. Each measurement takes three samples and keeps the one with the shortest round trip, because opening the connection (TLS) shifts the estimate by hundreds of ms. If a request is still refused with `-1021`, it measures again and retries once. That is safe even for orders, because Binance refuses before executing. So a clock that jumps while the agent runs (NTP turned back on, a VM that woke up) no longer requires restarting the agent. In the logs:

- `rest.clock_jumped`: the offset changed by more than 1 s between two measurements;
- `rest.timestamp_rejected`: a request was refused and retried.

A large, persistent *offset* in the "Offset de relógio (ms)" panel (the `trade_agent.clock.offset` metric) means the host clock is not synchronized. Turn on NTP (Windows: *Set time automatically*, with the *Windows Time* service running; Linux: `timedatectl set-ntp true`). On Windows with Rancher Desktop, the VM follows the Windows clock.

## 3. Telegram commands

| Command | Effect |
|---------|--------|
| `/status` | mode, states (global and per profile), positions and the analyst's latest reading |
| `/positions` · `/pnl [day\|week\|month]` · `/report` · `/config` | queries (`/pnl` without an argument: the last day) |
| `/pause [scope]` | no new entries; protections and rule-based exits go on |
| `/halt [scope]` | neither entries nor rule-based exits; protections kept |
| `/resume [scope]` | back to `running` (refused during a flatten) and lists the triggers that still hold (§2.3) |
| `/flatten [scope]` | cancels the protections and sells at market; asks for a confirmation code valid for 2 min |

Scope: `global` (default) or the profile name. Only the configured `TA_TELEGRAM_CHAT_ID` is served; messages from other chats are recorded as `telegram.unauthorized`.

## 4. Maintenance

- **Grafana's read-only user** on an existing database (the `deploy/postgres/init` script only runs on the first initialization of the volume):

  ```sql
  CREATE ROLE grafana_ro LOGIN PASSWORD '<GRAFANA_DB_PASSWORD>';
  GRANT CONNECT ON DATABASE trade_agent TO grafana_ro;
  GRANT USAGE ON SCHEMA public TO grafana_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
  ALTER DEFAULT PRIVILEGES FOR ROLE trade_agent IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
  ```

- **Dashboards:** edit `scripts/grafana_dashboards.py` and run `uv run python -m scripts.grafana_dashboards`. A test fails if the versioned JSON files get out of date.
- **Logs (Loki and Alloy):** the retention lives in `deploy/loki/loki.yaml` (`retention_period`), and the processing (labels, levels, tracebacks) in `deploy/alloy/config.alloy`. The tests run this pipeline for real in containers. Alloy reads the Docker API through a read-only proxy (`docker-proxy`), on an internal network only the collectors (Alloy and `container-metrics`) reach, because inspecting a container shows its environment variables (the `.env` keys). Do not give any service direct access to `docker.sock`. Three precautions kept in the configuration:
  - **Read position:** Alloy keeps how far it read each container (`alloy` volume) by the target's labels. That is why discovery keeps only stable labels (id, name and service). With IP, network or port in the key, every Docker restart made Alloy re-read the containers' whole log. Loki refused the old lines (`entry too far behind`), and the burst filled SigNoz's queue.
  - **Proxy:** `deploy/docker-proxy/haproxy.cfg.template` is the image's template plus a backend for `docker logs --follow`, without the default 10-minute cut. With the cut, a quiet container's log dropped every 10 minutes (`could not transfer logs` in Alloy), and each reconnection duplicated in SigNoz the lines of the last second read. Loki drops those copies, SigNoz does not. When updating the proxy image, a test compares the template with the new original.
  - **Copy to SigNoz:** goes out in batches. Alloy's own failures to send to SigNoz stay only in Loki. If those failures were copied, each one would become one more send to the full queue: when it came up before SigNoz, Alloy once logged millions of `sending queue is full` lines per hour. A burst of `Exporting failed` in Loki means SigNoz is down or slow.
  - **Dropped noise:** `docker_stats` (`container-metrics`) logs `Could not inspect updated container ... No such container` as an error whenever a container vanishes before being inspected. The lab creates and removes dozens per hour (`docker compose run --rm`), and compose does the same when recreating. That combination is dropped in Alloy (counter `loki_process_dropped_lines_total{reason="container_gone"}`); the collector's other errors keep going through.
- **Configuration:** `config/` is mounted into the agent's container. Edit the YAML files and run `docker compose ... restart agent` (no rebuild).
- **Restarting the agent:** the stop takes up to 30 s (`stop_grace_period`), the time to finish the task in progress. A decision cycle takes minutes and is interrupted; the startup reconciliation restores the state, but prefer to restart outside the first minutes after 00, 04, 08, 12, 16 and 20 h UTC (the 4h candle closes).
- **`.env`:** `deploy/docker-compose.yml` always reads the `.env` at the repository root and does not start without it, with or without `--env-file` and from any folder. The services live in `deploy/stack.yml`; do not start `stack.yml` directly, because it falls back to the default values and Grafana cannot log in to the database (`password authentication failed for user "grafana_ro"`). The `.env` is read only when the container is created, and `restart` does not read it again. After editing it, run `docker compose ... up -d` (recreates the agent and Grafana). For Telegram, `TA_TELEGRAM_CHAT_ID` is your user's id (private chat with the bot), and you need to send `/start` to the bot once before it can write to you.
- **Changing `managed_capital`:** it counts as neither a gain nor a loss. The day open and the peak follow the change in capital (`risk.equity_rebased` log), and only the trading result weighs on the daily loss and the drawdown. The percentages start being computed over the new capital.
- **Changing parameters:** always through the lab (`lab/walk_forward.py`) and *paper trading* on Demo before production.
- **Backup:** `docker compose ... exec postgres pg_dump -U trade_agent trade_agent | gzip > backup.sql.gz`, kept off the VPS.
