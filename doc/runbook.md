# Runbook de operação

> Procedimentos para quem opera o agente. O resumo está no [plano de construção](04-plano-de-construcao.md) (§4). **Regra de ouro:** as proteções (OCO) ficam na Binance e continuam valendo com o agente parado. Na dúvida, prefira `/halt` (mantém as proteções) a `/flatten` (vende tudo).

## 1. Subir e verificar

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d     # toda a pilha (agente e observabilidade)
docker compose --env-file .env -f deploy/docker-compose.yml logs -f agent
```

| Verificação | Onde | Esperado |
|-------------|------|----------|
| Agente iniciado | log `agent.started` / Telegram `/status` | reconciliação sem órfãs; estado `running` |
| Modo | `/status` (1ª linha) | "SIMULAÇÃO" enquanto `TA_TRADING_ENABLED=false` |
| Telemetria | Grafana → *Saúde técnica* → "Segundos desde a última foto" | abaixo de 360 s |
| Heartbeat | painel do Healthchecks.io | ping a cada minuto |
| Logs | Grafana → *Logs* | linhas chegando de todos os serviços, sem tracebacks |
| Traces | Jaeger → *System Architecture* | os componentes do agente ligados entre si |
| SigNoz | `http://127.0.0.1:8080` → *Services* e *Logs* | componentes do agente, logs de todos os serviços e métricas `trade_agent.*` |

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

**No Grafana (Loki):** o Alloy envia ao Loki os logs de todos os contêineres do projeto, que ficam guardados por 30 dias, inclusive depois de um `docker compose down`. O dashboard *Logs* filtra por serviço, nível e texto. Em *Explore* (fonte *Logs*), os rótulos são `service` e `level`, e o `event` do agente pode ser filtrado sem ler o JSON:

```logql
{service="agent", level=~"error|critical"}                          # erros e tracebacks do agente
{service="agent"} | event="risk.state_changed"                       # mudanças de estado de risco
{service="agent"} | json | event="decision.cycle" | profile="agressivo"
sum by (event) (count_over_time({service="agent"} | event!="" [1h]))  # eventos mais frequentes
```

Um traceback do Python chega como uma entrada só, com `level="error"`. No PostgreSQL, `FATAL: terminating connection due to administrator command` num reinício é esperado. A interface do Alloy (`http://127.0.0.1:12345`) mostra os contêineres descobertos e a saúde do pipeline. No Windows com Rancher Desktop, ela não abre (veja a nota sobre portas na §1.2).

### 1.2 Traces (OpenTelemetry e Jaeger)

Cada tarefa do agente vira um trace no Jaeger (`http://127.0.0.1:16686`, 7 dias em disco): a verificação de risco, a coleta de notícias, a reconciliação, cada ciclo de decisão, cada comando do Telegram e a partida. Cada componente aparece como um serviço (`trade-agent.runtime`, `.risk`, `.decision`, `.research`, `.llm`, `.execution`, `.exchange`, `.reconcile`, `.db`, `.telegram`, `.telemetry`), e a aba **System Architecture** desenha quem chama quem, com a contagem de chamadas.

| Onde | Para quê |
|------|----------|
| Jaeger → *Search* (serviço `trade-agent.runtime`, operação `job ...`) | a tarefa inteira, passo a passo, com a duração de cada chamada |
| Jaeger → *Search* (tag `error=true`) | traces com falha: exceção e mensagem no span |
| Grafana → *Explore* → *Traces* | o mesmo trace; em cada span, **Logs for this span** abre os logs no Loki |
| Grafana → *Explore* → *Logs* | um log do agente com `trace_id` mostra **Abrir trace** |

**O que cada span registra:** Binance (método, caminho sem query, status, peso usado), SQL (comando com `$1`, `$2`..., nunca os valores), LLM (modelo, finalidade, tokens, custo e motivo de parada, nunca o conteúdo), Telegram (método e comando, nunca o token nem o texto), decisão (perfil, avaliados, entradas, saídas e recusas), risco (patrimônio, gatilhos e mudanças de estado). O heartbeat e a espera por mensagens do Telegram não geram traces.

O Jaeger fica fixado na 2.20: a 2.21 removeu a API v1 que o datasource do Grafana usa (há um teste que falha se a versão mudar). Com o Jaeger fora do ar, o agente segue normalmente e descarta os spans.

**Uma porta não abre no Windows, mas o serviço está de pé?** Com Rancher Desktop, a conexão abre e a resposta nunca chega. Para confirmar, teste dentro da VM: `rdctl shell -- wget -qO- http://127.0.0.1:16686/` responde. O Rancher Desktop leva o tráfego do Windows até o contêiner com regras de NAT próprias (uma por IP do contêiner, na tabela `nat`, cadeia `DOCKER`), e há dois casos em que elas falham:

- **Regra antiga na frente:** quando um contêiner para, o Rancher Desktop não consegue apagar a regra dele, porque já não sabe o IP. No `rancher-desktop-guestagent.log` aparece `--delete DOCKER ... --to-destination :16686` com `Bad rule`. Se o contêiner volta com outro IP, a regra antiga vem primeiro e a porta para de responder. Foi o que aconteceu com a 16686 do Jaeger. Recriar o contêiner não resolve. Reinicie o Rancher Desktop (`rdctl shutdown` e abra de novo), que limpa as regras. Os contêineres voltam sozinhos (`restart: unless-stopped`).
- **Contêiner em várias redes (o Alloy, porta 12345):** o Rancher Desktop cria uma regra para cada rede, e vale a primeira. O Docker só aceita a porta publicada pela rede que escolheu para isso, e o tráfego que entra pelas outras é descartado. Reiniciar não resolve. A interface do Alloy fica inacessível no Windows, mas funciona num Linux com Docker comum, como a VPS. Para checar o Alloy no Windows, use `rdctl shell -- wget -qO- http://127.0.0.1:12345/-/ready` (dentro da VM) ou os logs `{service="alloy"}` no Grafana.

Enquanto a 16686 não abre, os traces continuam no Grafana, em *Explore* → *Traces*, que acessa o Jaeger pela rede interna.

### 1.3 SigNoz (traces, logs e métricas num lugar só)

O SigNoz roda em paralelo ao Jaeger e ao Loki, para comparar (`http://127.0.0.1:8080`). No primeiro acesso, ele pede para criar a conta de administrador. Ele recebe:

| Sinal | De onde | O que dá para ver |
|-------|---------|-------------------|
| Traces | o agente, com os mesmos spans do Jaeger | *Services*: latência, vazão e erros por componente e operação, calculados a partir dos spans; *Service Map*; custo do LLM por modelo (a partir dos tokens nos spans) |
| Logs | o Alloy, com uma cópia OTLP de tudo que vai para o Loki | busca por serviço, nível e qualquer campo do JSON do agente (`event`, `profile`, `symbol`...); cada log com `trace_id` abre o trace |
| Métricas do agente | o agente, a cada minuto | `trade_agent.equity`, `.drawdown`, `.pnl.*`, `.exposure`, `.positions.active`, `.binance.error_rate`, `.binance.weight_used_1m`, `.clock.offset`, `.risk.state` (0 operando, 1 pausado, 2 parado, 3 vendendo tudo); contadores `.llm.cost`, `.llm.tokens`, `.decision.cycles`, `.decision.entries`, `.decision.exits`, `.risk.state_changes` |
| Métricas dos contêineres | `container-metrics` (OpenTelemetry Collector, `docker_stats` pelo proxy somente leitura) | CPU, memória, rede e disco de cada contêiner do projeto |

Os logs internos do SigNoz (ClickHouse, keeper, migrações) não entram no Loki nem no SigNoz: veja com `docker compose ... logs <serviço>`. O agente envia traces aos dois destinos com filas separadas: um fora do ar não afeta o outro.

**Dashboards** (em *Dashboards*, com a etiqueta `projeto: trade-agent`):

| Dashboard | Para quê |
|-----------|----------|
| Trade Agent · Operação | patrimônio, resultado do dia, drawdown, exposição, posições e pior estado do risco agora; histórico de patrimônio (com abertura do dia e pico) e resultado; estado do risco por escopo; ciclos, entradas e saídas por perfil; os últimos ciclos de decisão |
| Trade Agent · Saúde técnica | falhas e peso da Binance, desvio do relógio, erros nos logs e tarefas com falha; latência p95 por endpoint da Binance, por tarefa e por operação do banco; respostas de erro por status; spans por componente; avisos e erros por serviço e os últimos do agente |
| Trade Agent · LLM | custo, tokens, chamadas e latência no período; custo por modelo e finalidade, tokens por direção, latência p50/p95 e as últimas chamadas |
| Trade Agent · Contêineres | CPU, memória, rede e disco por serviço do compose; memória em relação ao limite; linhas de log por serviço |

Os dashboards são código: o gerador `scripts/signoz_dashboards.py` grava os JSON em `deploy/signoz/dashboards/` e os aplica pela API. Um teste falha se os JSON ficarem desatualizados. Edite o gerador, não o dashboard na interface: aplicar de novo sobrescreve, pelo `name`, o que foi mudado à mão. A aplicação exige a chave de uma conta de serviço com papel *Editor* (*Settings → Service Accounts*) em `TA_SIGNOZ_API_KEY` no `.env`:

```bash
uv run python -m scripts.signoz_dashboards --apply   # cria ou atualiza os quatro dashboards
uv run python -m scripts.signoz_dashboards --check   # roda cada consulta nas últimas 24 h
```

No `--check`, um painel vazio pode ser só falta de eventos (nenhuma mudança de estado, nenhuma falha); um `ERRO` indica consulta inválida.

**Implantação:** os manifestos saem do Foundry, a ferramenta oficial do SigNoz, a partir de `deploy/signoz/casting.yaml` (versões fixadas). Para atualizar a versão, edite o casting e regenere; um teste falha se `pours/` ficar desatualizado:

```bash
docker run --rm -v "$PWD/deploy/signoz:/work" -w /work signoz/foundryctl:v0.3.0 forge --no-ledger --no-updater
```

Os ajustes locais (portas só em `127.0.0.1`, OTLP na 14318 para não colidir com o Jaeger, rotação de logs) ficam em `deploy/signoz/compose.override.yaml`. Na partida, um contêiner auxiliar baixa o `histogram-quantile` das releases oficiais do SigNoz no GitHub (função do ClickHouse), como na implantação oficial. O SigNoz usa mais memória que o resto da pilha (ClickHouse): numa VPS, reserve pelo menos 4 GB para ele.

## 2. Incidentes

### 2.1 Agente fora do ar (alerta "Agente sem telemetria" ou Healthchecks.io)

1. As posições seguem protegidas pelos OCO na Binance. Não há urgência para vender.
2. `docker compose ... ps` e `logs --tail 200 agent`. Procure `runtime.task_failed`, `AlreadyRunningError` e erros de banco. Se o contêiner foi removido, os logs anteriores à queda continuam no dashboard *Logs*. No Jaeger, a busca com `error=true` mostra em que passo a tarefa falhou.
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

O agente mede o desvio entre o relógio local e o da Binance na partida e a cada 10 minutos, e o compensa nas requisições assinadas. Cada medição usa três amostras e fica com a de menor ida e volta, porque abrir a conexão (TLS) desloca a estimativa em centenas de ms. Se uma delas ainda for recusada com `-1021`, ele mede de novo e repete uma vez. Isso é seguro mesmo para ordens, porque a Binance recusa antes de executar. Por isso, um relógio que pula com o agente rodando (NTP religado, VM que acordou) não exige mais reiniciar o agente. Nos logs:

- `rest.clock_jumped`: o desvio mudou mais de 1 s entre duas medições;
- `rest.timestamp_rejected`: uma requisição foi recusada e repetida.

Um *offset* grande e persistente no painel "Offset de relógio (ms)" (métrica `trade_agent.clock.offset`) indica que o relógio do host não está sincronizado. Ative o NTP (Windows: *Definir hora automaticamente*, com o serviço *Horário do Windows* em execução; Linux: `timedatectl set-ntp true`). No Windows com Rancher Desktop, a VM segue o relógio do Windows.

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
- **Logs (Loki e Alloy):** a retenção fica em `deploy/loki/loki.yaml` (`retention_period`), e o processamento (rótulos, níveis, tracebacks) em `deploy/alloy/config.alloy`. Os testes rodam esse pipeline de verdade em contêineres. O Alloy lê a API do Docker por um proxy somente leitura (`docker-proxy`), numa rede interna que só os coletores (Alloy e `container-metrics`) alcançam, porque a inspeção de um contêiner mostra as variáveis de ambiente (chaves do `.env`). Não dê a nenhum serviço acesso direto ao `docker.sock`. Três cuidados mantidos na configuração:
  - **Posição de leitura:** o Alloy guarda até onde leu cada contêiner (volume `alloy`) pelos rótulos do alvo. Por isso a descoberta só mantém rótulos estáveis (id, nome e serviço). Com IP, rede ou porta na chave, cada reinício do Docker fazia o Alloy reler o log inteiro dos contêineres. O Loki recusava as linhas antigas (`entry too far behind`), e a rajada enchia a fila do SigNoz.
  - **Proxy:** `deploy/docker-proxy/haproxy.cfg.template` é o template da imagem mais um backend para `docker logs --follow`, sem o corte de 10 minutos do padrão. Com o corte, o log de um contêiner quieto caía a cada 10 minutos (`could not transfer logs` no Alloy), e cada reconexão duplicava no SigNoz as linhas do último segundo lido. O Loki descarta essas cópias, o SigNoz não. Ao atualizar a imagem do proxy, um teste compara o template com o novo original.
  - **Cópia para o SigNoz:** sai em lotes. As falhas do próprio Alloy ao enviar ao SigNoz ficam só no Loki. Se essas falhas fossem copiadas, cada uma viraria mais um envio para a fila cheia: ao subir antes do SigNoz, o Alloy chegou a registrar milhões de linhas de `sending queue is full` por hora. Uma rajada de `Exporting failed` no Loki indica que o SigNoz está fora do ar ou lento.
- **Configuração:** `config/` é montado no contêiner do agente. Edite os YAML e rode `docker compose ... restart agent` (sem rebuild).
- **`.env`:** é lido só quando o contêiner é criado, e o `restart` não o relê. Depois de editar, rode `docker compose ... up -d` (recria o agente e o Grafana). Para o Telegram, `TA_TELEGRAM_CHAT_ID` é o id do seu usuário (chat privado com o bot), e é preciso enviar `/start` ao bot uma vez antes de ele conseguir escrever para você.
- **Mudança do `managed_capital`:** não conta como ganho nem perda. A abertura do dia e o pico acompanham a diferença de capital (log `risk.equity_rebased`), e só o resultado das operações pesa na perda diária e no drawdown. Os percentuais passam a ser calculados sobre o novo capital.
- **Mudança de parâmetros:** sempre via laboratório (`lab/walk_forward.py`) e *paper trading* no Demo antes de produção.
- **Backup:** `docker compose ... exec postgres pg_dump -U trade_agent trade_agent | gzip > backup.sql.gz`, guardado fora da VPS.
