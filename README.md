# AI Trade Agent

Agente autônomo de trade de criptomoedas na **Binance Spot**. Ele decide com base em análise técnica e em pesquisa de mercado (LLM) e protege cada posição com ordens nativas da Binance (OPOCO/OCO com trailing). A proteção continua valendo mesmo com o agente desligado.

- Arquitetura e plano: [`doc/`](doc/README.md)
- Estado atual: consulte o histórico de commits. Cada commit corresponde a uma fase do [plano de construção](doc/04-plano-de-construcao.md).

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

## Spike OPOCO (Fase 0)

Valida no Testnet/Demo os tipos de ordem dos quais a arquitetura depende:

```bash
uv run python scripts/spike_opoco.py --env-file .env           # usa BTCUSDT por padrão
uv run python scripts/spike_opoco.py --env-file .env --symbol ETHUSDT
```

O script recusa o ambiente `prod`. Os resultados brutos ficam em `var/spike/`.

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

O agente precisa de PostgreSQL e das chaves no `.env`. Na partida, ele aplica as migrações, sincroniza o relógio e reconcilia todas as posições com a Binance. Depois disso, reconcilia a cada 5 minutos e reage aos eventos do User Data Stream.

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
# baixa os candles (dados públicos) e roda o walk-forward com os parâmetros fixos do perfil
uv run python -m lab.walk_forward --profile conservador --start 2023-01-01 --end 2026-09-01 --download
# walk-forward otimizado: hyperopt nos 12 meses anteriores a cada janela trimestral de validação
uv run python -m lab.walk_forward --profile conservador --start 2024-01-01 --optimize --epochs 100 --download
```

Para comparar outra versão do código (A/B), aponte `TA_LAB_SRC` para a pasta `src/` dessa versão (ex.: um `git worktree`).

Os relatórios ficam em `var/lab/`. O resumo comentado está em [`doc/lab-resultados.md`](doc/lab-resultados.md).

## Estrutura

```text
src/trade_agent/      código da aplicação (ver doc/03-arquitetura-recomendada.md §4)
tests/unit/           testes unitários
tests/integration/    testes de integração (Binance simulada, Postgres em contêiner)
tests/live/           testes contra Testnet/Demo (marcador `live`)
scripts/              utilitários operacionais (spike etc.)
lab/                  laboratório de backtest (Freqtrade via Docker, walk-forward)
config/               perfis de alocação (profiles.yaml)
deploy/               docker-compose e provisionamento
doc/                  arquitetura, plano e decisões
```
