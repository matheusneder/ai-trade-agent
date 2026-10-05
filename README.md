# AI Trade Agent

Agente autônomo de trade de criptomoedas na **Binance Spot**. Ele decide com base em análise técnica e em pesquisa de mercado (LLM) e protege cada posição com ordens nativas da Binance (OPOCO/OCO com trailing). A proteção continua valendo mesmo com o agente desligado.

- Documentação: [`doc/`](doc/README.md) (arquitetura, operação e laboratório)
- Estado atual: *paper trading* no Demo Mode desde 29/09/2026; o que falta para produção está em [`doc/04-estado-e-go-live.md`](doc/04-estado-e-go-live.md)

> ⚠️ Software experimental. Não constitui recomendação de investimento. Use Testnet/Demo e, em produção, apenas capital que você aceita perder.

## Requisitos

- Python 3.12+ (desenvolvimento em 3.14)
- [uv](https://docs.astral.sh/uv/) (`pip install --user uv`; neste README os comandos usam `uv`, e `python -m uv` também funciona)
- Docker (testes de integração com PostgreSQL e implantação)

## Primeiros passos

```bash
uv sync                      # cria .venv e instala dependências (incluindo dev)
cp .env.example .env         # preencha com as chaves de Testnet/Demo
uv run pytest                # testes unitários e de integração (sem rede externa)
uv run pytest -m live        # testes contra Binance Testnet/Demo (exigem chaves no .env)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Chaves da Binance (Testnet / Demo)

1. Gere um par de chaves Ed25519 localmente e guarde a chave privada **fora do git** (a pasta `secrets/` é ignorada):

   ```bash
   mkdir -p secrets
   openssl genpkey -algorithm ed25519 -out secrets/binance-testnet-ed25519.pem
   openssl pkey -in secrets/binance-testnet-ed25519.pem -pubout
   ```

2. Cadastre a **chave pública** em:
   - Spot Testnet: https://testnet.binance.vision (login via GitHub → *Generate Ed25519 Key*)
   - Demo Mode: https://demo.binance.com → *API Management*
3. Preencha `TA_BINANCE_API_KEY` e `TA_BINANCE_PRIVATE_KEY_PATH` no `.env`.
4. Para permitir o envio de ordens, defina `TA_TRADING_ENABLED=true`. Sem isso, o cliente bloqueia qualquer ordem.

Nunca habilite permissão de saque nas chaves de API.

## Spike OPOCO

Valida num ambiente novo (Testnet ou Demo) os tipos de ordem dos quais a arquitetura depende:

```bash
uv run python scripts/spike_opoco.py --env-file .env           # usa BTCUSDT por padrão
uv run python scripts/spike_opoco.py --env-file .env --symbol ETHUSDT
```

O script recusa o ambiente `prod`. Os resultados brutos ficam em `var/spike/`.

Resultado no Spot Testnet (19/19, 28/09/2026) e achados sobre a API: [`doc/01-requisitos-e-binance.md`](doc/01-requisitos-e-binance.md) (§3).

## CLI de operação manual

Consultas e ordens protegidas (Testnet/Demo). O envio de ordens exige `TA_TRADING_ENABLED=true`; em produção, também `--confirm-prod`.

```bash
uv run trade-agent info BTCUSDT
uv run trade-agent account
uv run trade-agent lists
# compra de 20 USDT: OPOCO com TP em trailing (+3%, recuo 1%) e stop fixo (-4%)
uv run trade-agent open BTCUSDT --quote 20 --tp-pct 3 --tp-trailing-bips 100 --stop-pct 4
# proteger um saldo existente com OCO
uv run trade-agent protect BTCUSDT --qty 0.0003 --tp-pct 3 --tp-trailing-bips 100 --stop-pct 4
# encerrar: cancela a proteção e vende a mercado
uv run trade-agent close BTCUSDT --qty 0.0003 --list-id ta1-man-0a1b2c3d4e-0-L
```

## Executar o agente

O agente precisa de PostgreSQL e das chaves no `.env`. Na partida, ele aplica as migrações, sincroniza o relógio e reconcilia todas as posições com a Binance. Depois disso:

- reconcilia a cada 5 minutos e reage aos eventos do User Data Stream;
- avalia as condições de parada a cada minuto (`config/stop_conditions.yaml`);
- coleta notícias a cada 15 minutos;
- monta o universo e roda o ciclo de decisão de cada perfil habilitado no fechamento do candle (os perfis atuais usam 4h).

Use o **Demo Mode** (`TA_BINANCE_ENV=demo`) para rodar o agente: o Spot Testnet tem só ~20 dias de histórico, e o universo fica vazio (ver [`doc/runbook.md`](doc/runbook.md)). Com `TA_TRADING_ENABLED=false` (padrão), o agente roda em **simulação**: decide e registra as entradas e saídas como eventos, sem enviar ordens. Com o Telegram configurado (`TA_TELEGRAM_BOT_TOKEN` e `TA_TELEGRAM_CHAT_ID`), ele envia alertas e aceita `/status`, `/pause`, `/resume`, `/halt` e `/flatten` (este com código de confirmação).

```bash
docker compose -f deploy/docker-compose.yml up -d postgres   # banco local
uv run trade-agent run                                        # ou: docker compose ... up -d agent
```

Só pode haver uma instância ativa por banco (*advisory lock*). Migrações manuais com Alembic:

```bash
uv run alembic -x url=postgresql+asyncpg://trade_agent:trade_agent_dev@localhost:5432/trade_agent upgrade head
```

## Laboratório de backtest (Freqtrade via Docker)

A estratégia "casca" do Freqtrade importa o mesmo pacote de sinais usado em produção (`trade_agent.signals`). A configuração e os parâmetros são gerados a partir de `config/profiles.yaml`.

```bash
# walk-forward otimizado: hyperopt nos 12 meses anteriores a cada janela trimestral de validação
uv run python -m lab.walk_forward --profile swing_trend --optimize --download
# com os parâmetros atuais do perfil, sem otimizar
uv run python -m lab.walk_forward --profile momentum_alpha
```

Para comparar outra versão do código (A/B), aponte `TA_LAB_SRC` para a pasta `src/` dessa versão (ex.: um `git worktree`).

As janelas vão de 2023-01-01 a 2026-09-01 por padrão (`--start`, `--end`). Os relatórios ficam em `var/lab/`, e o resumo comentado da calibração em uso está em [`doc/lab-resultados.md`](doc/lab-resultados.md).

## Analista de mercado

Coleta notícias e métricas públicas, faz a triagem com `claude-sonnet-5` e produz uma leitura de mercado (`MarketView`) com `claude-opus-5`. As regras de segurança são aplicadas por código: o LLM só pode vetar ativos e reduzir a exposição. Modelos, fontes, orçamento (US$ 5/dia) e limites ficam em `config/research.yaml`; a chave vai no `.env` (`ANTHROPIC_API_KEY`).

```bash
uv run trade-agent research ingest                     # coleta notícias e métricas (PostgreSQL)
uv run trade-agent research run --assets BTC,ETH,SOL   # um ciclo de pesquisa
uv run trade-agent research show                       # última leitura válida
uv run trade-agent research eval                       # avaliação com 31 casos rotulados (~US$ 0,60)
```

## Observabilidade

O agente grava uma foto de telemetria a cada 5 minutos (`telemetry_snapshots`) e envia um heartbeat a cada minuto para a URL de `TA_HEALTHCHECK_URL` (ex.: Healthchecks.io). O Grafana sobe junto no compose, com 6 dashboards (visão geral, posições, performance, decisões e pesquisa, saúde técnica e logs) e alertas no Telegram. Acesse em `http://127.0.0.1:3000` (admin / `GRAFANA_ADMIN_PASSWORD`). Os logs de todos os contêineres vão para o Loki (coletados pelo Grafana Alloy, guardados por 30 dias). Os traces do agente (OpenTelemetry) vão para o Jaeger (7 dias), com cada componente como serviço, e aparecem no Grafana em *Explore* → *Traces*; logs e traces se ligam nos dois sentidos pelo `trace_id`. O grafo de dependências também está no Grafana (fonte Jaeger, *Dependency graph*). A interface do próprio Jaeger só abre no host em desenvolvimento, com `DEV_JAEGER_UI=1` (runbook, §1.2). Em paralelo, o SigNoz (`http://127.0.0.1:8080`) reúne traces, logs e métricas: as do agente (patrimônio, drawdown, estado do risco, custo do LLM...) e as de cada contêiner, com 4 dashboards (operação, saúde técnica, LLM e contêineres) gerados por `scripts/signoz_dashboards.py`. Procedimentos de incidente: [`doc/runbook.md`](doc/runbook.md).

```bash
docker compose -f deploy/docker-compose.yml up -d postgres grafana   # lê o .env da raiz
uv run python -m scripts.grafana_dashboards   # regenera os dashboards após editar o gerador
```

## Estrutura

```text
src/trade_agent/      código da aplicação (ver doc/03-arquitetura.md §4)
tests/unit/           testes unitários
tests/integration/    testes de integração (Binance simulada, Postgres em contêiner)
tests/live/           testes contra Testnet/Demo (marcador `live`)
scripts/              spike OPOCO e geradores dos dashboards do Grafana e do SigNoz
lab/                  laboratório de backtest (Freqtrade via Docker, walk-forward)
config/               perfis de alocação (profiles.yaml) e analista (research.yaml)
evals/                casos rotulados para avaliar o analista LLM
deploy/               docker-compose e provisionamento
doc/                  arquitetura, plano e decisões
```
