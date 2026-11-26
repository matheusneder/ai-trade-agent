# Moving the agent to Oracle Cloud (OCI)

> A proposal for open decision 1 of [doc 04](04-status-and-go-live.md) (VPS provider and region) and the procedure to move the running stack there. The quotas and prices below were checked on 2026-11-26.

## 1. Why OCI

- **Load measured on 2026-11-26** (Windows host, Rancher Desktop):
  - Memory: the 14 containers use about 3.8 GB. SigNoz takes 2.2 GB of that (ClickHouse alone, 1.7 GB), the agent 270 MB and its PostgreSQL 140 MB.
  - CPU: close to idle. ClickHouse uses about 9% and the agent about 0% between cycles.
  - Disk: the volumes hold about 5 GB (Jaeger 2.9 GB, ClickHouse 1.3 GB). The agent's database is 89 MB.
- **What the free allowance covers:** OCI's Always Free Ampere A1 allowance (1,500 OCPU-hours and 9,000 GB-hours a month) keeps one VM with 2 OCPUs and 12 GB running all month, with 200 GB of block storage. That runs the whole compose stack, unchanged, with three times the memory it uses today.
- **ARM (`aarch64`), checked on 2026-11-26:**
  - Images: every image in `deploy/stack.yml` and in the SigNoz manifests publishes `linux/arm64`, and so do the build's `python:3.15.2-slim`, `uv` and Dockerfile frontend.
  - Python packages: every compiled package in `uv.lock` has an `aarch64` wheel, except `pyyaml`, which builds from source on both architectures, as it already does on x86.
- **Discarded:**
  - Google Cloud's free `e2-micro` exists only in US regions, so Binance answers HTTP 451 (doc 01, §2.5), and it has 1 GB.
  - AWS gives new accounts credits for 6 months, not a free tier.
  - Serverless (Lambda with Aurora) does not fit: the agent's WebSocket and Telegram polling need a long-running process, and the fixed IP needs a NAT gateway (about US$ 33/month) besides the database. It would cost more than a VM after a rewrite.
- **Paid fallback, same architecture:** Hetzner CAX21 (4 ARM vCPUs, 8 GB, about €11/month with IPv4 since the June 2026 price increase).

## 2. Rules for the move

- **One agent at a time.** The instance lock (a PostgreSQL advisory lock) only stops a second agent on the *same* database. An agent on the VM with its own database, running beside the local one, would manage the same Binance account twice: duplicate entries and two agents adjusting the same protections. Both would also poll Telegram, and `getUpdates` answers 409 to the second poller. So the local agent stops before the dump, and the local stack never comes back up with this `.env` afterwards.
- **Positions stay protected:** the OCOs on Binance keep working while no agent runs. The startup reconciliation picks up whatever filled in the meantime (runbook, §2.1).
- **What moves:** the database moves: positions, intents, fills, events and the A/B tape.
- **What does not move:** the telemetry history (Loki, Jaeger, SigNoz) and Grafana's internal state start empty on the VM. The local volumes stay on the Windows machine until you remove them.

## 3. Account and network (OCI console)

1. **Sign up.** The **home region** is chosen at sign-up and cannot be changed later. Always Free resources exist only in the home region. Choose **Brazil East (São Paulo)**, `sa-saopaulo-1`, or **Brazil Southeast (Vinhedo)**, `sa-vinhedo-1`. A Brazilian IP also matches the Binance account's country.
2. **Upgrade the account to Pay As You Go** (*Billing & Cost Management → Upgrade and Manage Payment*).
   - Usage inside the Always Free limits stays free.
   - The upgrade raises the priority for A1 capacity: *Out of host capacity* is common in the Brazilian regions.
   - It also exempts the instance from idle-instance reclamation (per user reports). An Always Free instance whose 95th-percentile CPU stays below 20% for 7 days may be reclaimed, and this stack runs below that.
3. **Create a budget** (*Billing & Cost Management → Budgets*) of US$ 1, with an alert at 100%. Any charge means something left the free allowance.
4. **Create the network:** *Networking → Virtual cloud networks → Start VCN Wizard → Create VCN with Internet Connectivity*. Keep the public subnet's default security list, which only allows inbound TCP 22. Open nothing else: Grafana, SigNoz and PostgreSQL listen on `127.0.0.1` and are reached through an SSH tunnel (§9).

## 4. The VM

*Compute → Instances → Create instance*:

| Field | Value |
|-------|-------|
| Image | Canonical Ubuntu, the latest LTS, `aarch64` |
| Shape | `VM.Standard.A1.Flex`, 2 OCPUs, 12 GB |
| Networking | the VCN's public subnet, with a public IPv4 |
| SSH keys | paste your public key (`ssh-keygen -t ed25519` creates one) |
| Boot volume | 100 GB (leaves 100 GB of the free 200 GB) |

If the creation fails with *Out of host capacity*, pick another fault domain or retry later. After the Pay As You Go upgrade it usually goes through.

**Make the IP fixed.** The production API key will be restricted to this address (doc 01, §2.5), so it has to survive recreating the instance. An ephemeral IP is lost when the instance is terminated. To switch, go to *Instance → Attached VNICs → the VNIC → IPv4 addresses → Edit*. Set it to *No public IP*, then to *Reserved public IP → Create new*. Community reports say free accounts are limited to one reserved IP. The budget alert flags it if the IP is billed.

## 5. Host setup

```bash
ssh ubuntu@<IP>
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu   # log out and back in, then: docker run --rm hello-world
git clone https://github.com/matheusneder/ai-trade-agent.git ~/ai-trade-agent
```

Ubuntu's unattended upgrades come on by default: security updates, no automatic reboot. After a reboot, the containers come back on their own (`restart: unless-stopped`).

**Copy the `.env`** from the Windows machine, from the repository root (PowerShell or Git Bash):

```bash
scp .env ubuntu@<IP>:ai-trade-agent/.env
```

On the VM, run `chmod 600 ~/ai-trade-agent/.env`, then edit the file:

- Delete `DEV_JAEGER_UI`, `DEV_GRAFANA_UI_HOST` and `DEV_SIGNOZ_UI_HOST`. They are for development only.
- Add `TA_POSTGRES_PASSWORD` with a strong password. PostgreSQL reads it only at the first initialization of its volume, and the VM starts from an empty one. The local default, `trade_agent_dev`, stays on the local machine.
- Optional, but useful now that the agent runs elsewhere: `TA_HEALTHCHECK_URL` (go-live checklist).
- `TA_SIGNOZ_API_KEY` belongs to the local SigNoz and is not used on the VM (§6).

The Demo key in use is HMAC, so there is nothing else to copy. An Ed25519 key (`TA_BINANCE_KEY_TYPE=ed25519`) also needs its PEM. Copy `secrets/` and hand the folder to the container's user (uid 10001), which reads it through the read-only mount:

```bash
scp -r secrets ubuntu@<IP>:ai-trade-agent/                       # on Windows
sudo chown -R 10001:10001 ~/ai-trade-agent/secrets && sudo chmod 700 ~/ai-trade-agent/secrets && sudo chmod 600 ~/ai-trade-agent/secrets/*   # on the VM
```

## 6. Rehearsal (the local agent keeps trading)

Build the agent's image and check Binance from the VM. A ping returns 200 when the region is allowed and 451 when it is restricted. `account` and `lists` are read-only and need no database. They prove that the image runs on ARM and that the key works from the new IP: a key restricted to another IP answers `-2015`.

```bash
cd ~/ai-trade-agent
docker compose -f deploy/docker-compose.yml build agent
for h in api.binance.com demo-api.binance.com; do curl -s -o /dev/null -m 10 -w "$h: %{http_code}\n" https://$h/api/v3/ping; done
docker compose -f deploy/docker-compose.yml run --rm --no-deps agent trade-agent account
docker compose -f deploy/docker-compose.yml run --rm --no-deps agent trade-agent lists
```

Bring up the stack without the agent and without Grafana. Grafana's *Agente sem telemetria* rule would alert on Telegram while the database is still empty.

```bash
docker compose -f deploy/docker-compose.yml up -d --scale agent=0 --scale grafana=0
docker compose -f deploy/docker-compose.yml ps
free -h
```

**SigNoz dashboards on the VM:**

1. Open the tunnel (§9) and go to `http://127.0.0.1:18080`.
2. Create the admin account.
3. Create a service account with the *Editor* role (*Settings → Service Accounts*).
4. From the Windows checkout, apply the dashboards with that key. Environment variables take precedence over the `.env`, so the local `.env` stays as it is:

   ```bash
   TA_SIGNOZ_URL=http://127.0.0.1:18080 TA_SIGNOZ_API_KEY=<key of the VM's SigNoz> uv run python -m scripts.signoz_dashboards --apply
   ```

**Optional dump and restore drill.** It times the cutover and catches restore errors early. Take the dump with the commands of §7, step 3, without stopping the agent. Then, on the VM:

```bash
docker compose -f deploy/docker-compose.yml cp ~/cutover.dump postgres:/tmp/cutover.dump
docker compose -f deploy/docker-compose.yml exec postgres createdb -U trade_agent trade_agent_rehearsal
docker compose -f deploy/docker-compose.yml exec postgres pg_restore -U trade_agent -d trade_agent_rehearsal --no-owner --exit-on-error /tmp/cutover.dump
docker compose -f deploy/docker-compose.yml exec postgres psql -U trade_agent -d trade_agent_rehearsal -c "select version_num from alembic_version"
docker compose -f deploy/docker-compose.yml exec postgres dropdb -U trade_agent trade_agent_rehearsal
```

## 7. Cutover

**When:** outside the hour of a 4h close (00, 04, 08, 12, 16 and 20 UTC). Start right after the `decision.cycle` log lines of a 15-minute close (:00:20, :15:20, :30:20 or :45:20). The steps take 5 to 10 minutes, and the cost is at most one skipped 15m cycle.

On Windows, from the repository root:

1. **Record the state:** send `/status` on Telegram, and list the protections that must still exist on the VM:

   ```bash
   docker compose -f deploy/docker-compose.yml exec agent trade-agent lists
   ```

2. **Stop the agent:** `docker compose -f deploy/docker-compose.yml stop agent`. Its log ends with `agent.stopped`.
3. **Dump the database.** The dump is written inside the container and copied out, so no shell touches the binary stream. In Git Bash, prefix the first command with `MSYS_NO_PATHCONV=1`: without it, Git Bash rewrites `/tmp/cutover.dump` into a Windows path before Docker sees it. PowerShell needs nothing.

   ```bash
   docker compose -f deploy/docker-compose.yml exec postgres pg_dump -U trade_agent -d trade_agent -Fc -f /tmp/cutover.dump
   docker compose -f deploy/docker-compose.yml cp postgres:/tmp/cutover.dump var/cutover.dump
   ```

4. **Stop the local stack:** `docker compose -f deploy/docker-compose.yml down`. This keeps the volumes, so the local database stays as the pre-cutover copy for a rollback. It also stops the local Grafana before its *Agente sem telemetria* rule fires. From here on, do not run `up` locally with this `.env` (§2).
5. **Copy the dump to the VM:** `scp var/cutover.dump ubuntu@<IP>:`

On the VM:

6. **Restore into the empty database.** The first initialization created `grafana_ro` with `GRAFANA_DB_PASSWORD`, and the dump carries its grants.

   ```bash
   cd ~/ai-trade-agent
   docker compose -f deploy/docker-compose.yml cp ~/cutover.dump postgres:/tmp/cutover.dump
   docker compose -f deploy/docker-compose.yml exec postgres pg_restore -U trade_agent -d trade_agent --no-owner --exit-on-error /tmp/cutover.dump
   docker compose -f deploy/docker-compose.yml exec postgres psql -U trade_agent -d trade_agent -c "select version_num from alembic_version"
   ```

7. **Start everything,** the agent and Grafana included. If `main` moved since the rehearsal, run `git pull` first and add `--build`.

   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   docker compose -f deploy/docker-compose.yml logs -f agent
   ```

## 8. Checks after the cutover

- **Startup:** the log shows `agent.started`, with the reconciliation and no `reconcile.orphan_list`.
- **Positions:** `/status` answers with the same positions as before. Only the VM's agent polls Telegram now. `docker compose -f deploy/docker-compose.yml exec agent trade-agent lists` shows the same protections as step 1.
- **Telemetry:** the runbook's table of checks (§1) passes. Telemetry is below 360 s, and logs and traces arrive in Grafana and SigNoz. Run the port loop there on the VM.
- **Decisions:** the next close produces `decision.cycle` lines.
- **Cost:** after 24 h, the budget still shows US$ 0.

With positions open during the move, the cutover also covers the checklist's "at least one restart with open positions".

## 9. Operating on the VM

- **Access:** SSH tunnel from Windows, on local ports that do not clash with a local stack:

  ```bash
  ssh -N -L 13000:127.0.0.1:3000 -L 18080:127.0.0.1:8080 ubuntu@<IP>
  ```

  Grafana is then at `http://127.0.0.1:13000` and SigNoz at `http://127.0.0.1:18080`. For the phone, use a private network (e.g. Tailscale) instead of opening ports.
- **Updates:** the runbook's procedures (§1 and §4) run unchanged on the VM, with `git pull` in `~/ai-trade-agent`.
- **Configuration:** changes to `config/` go through a PR and a pull, never an edit on the VM, because the agent mounts that checkout's `config/`.
- **What stays on the development machine:** the lab, the tests and the dashboard generators. The VM only runs the stack.
- **Rollback:** stop the VM's agent first (`docker compose -f deploy/docker-compose.yml stop agent` on the VM).
  - If it has not opened or closed a position yet, `docker compose -f deploy/docker-compose.yml up -d` on Windows resumes from the pre-cutover database, and the reconciliation catches up with Binance.
  - Otherwise, reverse §7: dump on the VM and restore locally into a new, empty volume.
- **Still open in the go-live checklist (doc 04, §2):**
  - The automated, encrypted, off-machine backup. OCI Object Storage has an Always Free allowance, and `scripts.backup` encrypts.
  - The production key restricted to the reserved IP.
  - The heartbeat tested with the machine off.
