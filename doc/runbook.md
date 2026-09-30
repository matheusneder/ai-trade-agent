# Runbook de operação

> Procedimentos para quem opera o agente. O resumo está no [plano de construção](04-plano-de-construcao.md) (§4). **Regra de ouro:** as proteções (OCO) ficam na Binance e continuam valendo com o agente parado. Na dúvida, prefira `/halt` (mantém as proteções) a `/flatten` (vende tudo).

## 1. Subir e verificar

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d     # postgres, agente e grafana
docker compose --env-file .env -f deploy/docker-compose.yml logs -f agent
```

| Verificação | Onde | Esperado |
|-------------|------|----------|
| Agente iniciado | log `agent.started` / Telegram `/status` | reconciliação sem órfãs; estado `running` |
| Modo | `/status` (1ª linha) | "SIMULAÇÃO" enquanto `TA_TRADING_ENABLED=false` |
| Telemetria | Grafana → *Saúde técnica* → "Segundos desde a última foto" | abaixo de 360 s |
| Heartbeat | painel do Healthchecks.io | ping a cada minuto |

**Ambiente:** o **Spot Testnet** serve para validar ordens (spike e `pytest -m live`), mas não para o ciclo de decisão. Ele é reiniciado periodicamente e tem só ~20 dias de histórico (os sinais precisam de 201 candles e o universo, de 30 dias), além de volumes artificiais. O universo fica vazio e o log mostra `decision.empty_universe`. Para o *paper trading*, use o **Demo Mode** (`TA_BINANCE_ENV=demo`, com chaves criadas em demo.binance.com), que usa dados reais de mercado.

As tarefas periódicas (risco, telemetria, notícias e heartbeat) rodam já na partida e depois a cada intervalo. O primeiro ciclo de decisão acontece no próximo fechamento do candle de 4h (00, 04, 08, 12, 16 e 20 UTC).

O Grafana escuta só em `127.0.0.1:3000`. Numa VPS, use um túnel: `ssh -L 3000:127.0.0.1:3000 usuario@vps`.

### 1.1 Logs e nível DEBUG

`TA_LOG_LEVEL=DEBUG` no `.env` (depois `docker compose ... up -d agent`) mostra o detalhe de cada etapa. `INFO` volta ao resumo. Os logs em JSON trazem um campo `event`, filtrável:

| Evento | O que mostra |
|--------|--------------|
| `rest.request` / `rest.request_failed` | método, caminho (sem *query*), status, latência, peso usado |
| `risk.snapshot` / `risk.evaluated` / `risk.hit_unchanged` | patrimônio, abertura do dia, pico, BTC 1h, paridade, erros de API; gatilhos atingidos |
| `decision.cycle_start` / `decision.signal` / `decision.reading` / `decision.plan` / `decision.exit_check` / `decision.pre_trade_rejected` | universo elegível, score e setup por ativo, leitura do analista, ideias e recusas, checagem de saída |
| `position.sync` / `order.*` / `order_list.*` / `protection.replace` | veredito de cada sincronização e o caminho de cada ordem |
| `reconcile.done` / `reconcile.position` / `reconcile.intent` | resumo e detalhe de cada reconciliação |
| `llm.request` / `llm.response` / `analyst.*` / `research.*` | modelo, tokens, custo e regime (nunca o conteúdo); fontes e notícias coletadas |
| `runtime.job` / `runtime.task_failed` / `schedule.next_run` | duração de cada tarefa, falhas com o nome da tarefa, próximo ciclo de decisão |
| `telegram.*` / `alert.sent` / `heartbeat.ok` / `telemetry.recorded` | comandos e chamadas ao Telegram, alertas, heartbeat e telemetria |

```bash
docker compose --env-file .env -f deploy/docker-compose.yml logs -f agent | grep -E '"event": "(risk|decision)\.'
```

Segredos nunca vão para os logs: chaves, assinaturas, senhas e tokens são mascarados (`***`) e as bibliotecas HTTP (que registram URLs com tokens) ficam em `WARNING`.

## 2. Incidentes

### 2.1 Agente fora do ar (alerta "Agente sem telemetria" ou Healthchecks.io)

1. As posições seguem protegidas pelos OCO na Binance. Não há urgência para vender.
2. `docker compose ... ps` e `logs --tail 200 agent`. Procure `runtime.task_failed`, `AlreadyRunningError` e erros de banco.
3. Reinicie com `docker compose ... restart agent`. A partida aplica as migrações, sincroniza o relógio e **reconcilia** tudo: intenções pendentes, posições e órfãs.
4. Confira `/status` e o painel *Posições*.

### 2.2 Posição sem proteção (alerta crítico)

O agente tenta re-proteger na hora. Se o novo OCO for rejeitado, ele vende a mercado (*fail-safe*, D-007). Se o alerta persistir:

1. Veja o evento no painel *Saúde técnica* → "Eventos altos e críticos" (ex.: `EXECUTION_RULE_PRICE_RANGE_EXCEEDED`, filtro, saldo).
2. `/halt <perfil>` para bloquear novas entradas enquanto investiga.
3. Proteja manualmente com `uv run trade-agent protect SIMBOLO --qty ... --tp-pct ... --tp-trailing-bips ... --stop-pct ...` ou encerre com `trade-agent close`.

### 2.3 Disjuntor acionado (`paused`, `halted`)

| Estado | O que significa | O que fazer |
|--------|-----------------|-------------|
| `paused` com prazo | pausa automática (ex.: queda do BTC, Fear & Greed, erros de API) | nada; volta sozinho no fim do *cooldown* |
| `paused` sem prazo | divergência na reconciliação ou `/pause` | diagnosticar e `/resume <escopo>` |
| `halted` | drawdown do pico, meta atingida ou `/halt` | analisar a causa no painel *Visão geral* e `/resume` quando seguro |
| `halted` após flatten | *depeg* da moeda de cotação ou `/flatten` | confirmar que as vendas saíram (painel *Posições*) antes do `/resume` |

### 2.4 IP banido (HTTP 418) ou excesso de requisições (429)

Os erros trazem o `Retry-After` informado pela Binance, e a taxa de falhas de infraestrutura acima de 20% em 5 min pausa as entradas por 30 min. Em caso de 418, pare o agente (`/halt` e `docker compose ... stop agent`) até o fim do banimento. Revise o painel "Peso usado (1 min)" e reduza a frequência de tarefas se necessário.

### 2.5 Analista LLM indisponível ou sem orçamento

Cada perfil segue o seu `llm.on_failure` (`ta_only`, `ta_only_reduced` ou `pause_entries`). Veja o painel *Decisões e pesquisa* ("Ciclos do analista por status", "Custo diário do LLM"). O teto diário fica em `config/research.yaml` (`budget.daily_usd`).

### 2.6 Relógio fora de sincronia (`-1021 Timestamp outside recvWindow`)

O agente sincroniza o relógio na partida. Um *offset* persistente no painel "Offset de relógio (ms)" indica problema no host. Ative o NTP (Windows: *Definir hora automaticamente*; Linux: `timedatectl set-ntp true`) e reinicie o agente.

## 3. Comandos do Telegram

| Comando | Efeito |
|---------|--------|
| `/status` | modo, estados (global e por perfil), posições e última leitura do analista |
| `/positions` · `/pnl [dia\|semana\|mes]` · `/report` · `/config` | consultas |
| `/pause [escopo]` | sem novas entradas; proteções e saídas por regra continuam |
| `/halt [escopo]` | sem entradas nem saídas por regra; proteções mantidas |
| `/resume [escopo]` | volta a `running` (recusado durante um flatten) |
| `/flatten [escopo]` | cancela as proteções e vende a mercado; pede um código de confirmação válido por 2 min |

Escopo: `global` (padrão) ou o nome do perfil. Só o `TA_TELEGRAM_CHAT_ID` configurado é atendido; mensagens de outros chats são registradas como `telegram.unauthorized`.

## 4. Manutenção

- **Usuário somente leitura do Grafana** num banco já existente (o script de `deploy/postgres/init` só roda na primeira inicialização do volume):

  ```sql
  CREATE ROLE grafana_ro LOGIN PASSWORD '<GRAFANA_DB_PASSWORD>';
  GRANT CONNECT ON DATABASE trade_agent TO grafana_ro;
  GRANT USAGE ON SCHEMA public TO grafana_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
  ALTER DEFAULT PRIVILEGES FOR ROLE trade_agent IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
  ```

- **Dashboards:** edite `scripts/grafana_dashboards.py` e rode `uv run python -m scripts.grafana_dashboards`. Um teste falha se os JSON versionados ficarem desatualizados.
- **Configuração:** `config/` é montado no contêiner do agente. Edite os YAML e rode `docker compose ... restart agent` (sem rebuild).
- **`.env`:** é lido só quando o contêiner é criado, e o `restart` não o relê. Depois de editar, rode `docker compose ... up -d` (recria o agente e o Grafana). Para o Telegram, `TA_TELEGRAM_CHAT_ID` é o id do seu usuário (chat privado com o bot), e é preciso enviar `/start` ao bot uma vez antes de ele conseguir escrever para você.
- **Mudança do `managed_capital`:** não conta como ganho nem perda. A abertura do dia e o pico acompanham a diferença de capital (log `risk.equity_rebased`), e só o resultado das operações pesa na perda diária e no drawdown. Os percentuais passam a ser calculados sobre o novo capital.
- **Mudança de parâmetros:** sempre via laboratório (`lab/walk_forward.py`) e *paper trading* no Demo antes de produção.
- **Backup:** `docker compose ... exec postgres pg_dump -U trade_agent trade_agent | gzip > backup.sql.gz`, guardado fora da VPS.
