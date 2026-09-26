# 01 — Contexto, requisitos e pesquisa

> Pesquisa realizada em **25/09/2026**. Os fatos sobre a API da Binance foram conferidos na documentação oficial (`binance-spot-api-docs`, changelog atualizado em 18/09/2026) e no endpoint público `exchangeInfo`. Links completos em [05-referencias.md](05-referencias.md).

## 1. Leitura dos requisitos

| # | Requisito | O que implica na arquitetura | Decisão-chave |
|---|-----------|------------------------------|---------------|
| R1 | Autonomia para escolher pares e comprar/vender | Seleção dinâmica de universo + ranqueamento + execução sem intervenção humana | Pipeline determinístico de seleção (filtros de liquidez e risco) + ranking por score técnico e de sentimento |
| R2 | Perfis de alocação (conservador, moderado, agressivo) | Parâmetros de risco, universo e tamanho de posição por perfil | Perfis declarativos em YAML versionado, validados por schema |
| R3 | Decisões baseadas em análise técnica, notícias e sentimento | Duas fontes de sinal com naturezas distintas: numérica (TA) e textual (notícias) | TA em código determinístico; notícias e sentimento com LLM, saída estruturada e limitada |
| R4 | Ordens com gatilho (OCO etc.) que protegem **mesmo com o agente desligado** | A proteção precisa ficar **no servidor da Binance**, e não no processo do agente | Usar *order lists* nativas (OPOCO/OTOCO/OCO) com `trailingDelta` nativo |
| R5 | Condições de parada parametrizáveis | Disjuntores (*circuit breakers*) e *kill switch* persistentes | Máquina de estados de operação (RUNNING → PAUSED → HALTED) com gatilhos em YAML |
| R6 | Monitoramento, alertas, painéis | Telemetria, dashboards, notificações push e comandos remotos | Grafana sobre o banco + Telegram (alertas e comandos) + *heartbeat* externo |
| R7 | Resiliência, persistência e recuperação automática | Estado durável, idempotência e reconciliação com a exchange | A exchange é a fonte da verdade; o banco local guarda diário e intenções; reconciliação na partida e periódica |
| R8 | Simplicidade, componentes maduros e open source | Mínimo de peças móveis | 1 serviço Python + PostgreSQL + Grafana; bibliotecas maduras para tudo que não é regra de negócio |
| R9 | Considerar os insights iniciais sem se limitar a eles | Avaliar LangGraph/MCP, Freqtrade/FreqAI, Hummingbot, OctoBot e alternativas | Ver comparação na seção 4 e em [02-propostas-de-arquitetura.md](02-propostas-de-arquitetura.md) |

**Premissas adotadas** (validar na seção “Decisões em aberto” do plano):

- Mercado **Spot**, **sem alavancagem** (conforme recomendação dos insights). Em Spot só existe posição comprada (*long-only*). “Vender” significa sair da posição e voltar para a moeda de cotação (stablecoin).
- Operação de **swing/intraday lento**, com candles de 1h a 4h. Não é HFT nem scalping. Taxas e a latência do LLM inviabilizam frequências altas.
- Moeda de cotação padrão **USDT** (maior liquidez). **USDC** ou **BRL** são configuráveis: no dia da pesquisa havia 496 pares USDT, 258 USDC e 18 BRL em negociação.

## 2. O que a Binance oferece (e que muda o desenho)

### 2.1 Ordens condicionais nativas (*order lists*)

A Binance Spot oferece hoje cinco tipos de *order list*. O endpoint antigo `POST /api/v3/order/oco` está **depreciado** e foi substituído por `POST /api/v3/orderList/oco`.

| Tipo | Endpoint REST | Como funciona | Uso no agente |
|------|---------------|---------------|---------------|
| **OCO** | `POST /api/v3/orderList/oco` | Duas ordens (acima/abaixo do preço); quando uma executa, a outra é cancelada. `aboveType`: `STOP_LOSS_LIMIT`, `STOP_LOSS`, `LIMIT_MAKER`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`. `belowType`: `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`. Aceita `aboveTrailingDelta` e `belowTrailingDelta`. | Proteção de posição já aberta (re-proteção, ajuste de stop) |
| **OTO** | `POST /api/v3/orderList/oto` | Ordem de trabalho (`LIMIT`/`LIMIT_MAKER`) que, ao executar **totalmente**, dispara uma ordem pendente | Pouco útil sozinha |
| **OTOCO** | `POST /api/v3/orderList/otoco` | Ordem de entrada que, ao executar totalmente, coloca um **par OCO** (pendente acima + pendente abaixo) | Entrada + TP + SL em uma única chamada |
| **OPO** | `POST /api/v3/orderList/opo` | Igual ao OTO, mas a ordem pendente usa **a quantidade efetivamente recebida** na entrada, já descontada a comissão | — |
| **OPOCO** | `POST /api/v3/orderList/opoco` | Igual ao OTOCO, com quantidade = quantidade recebida. **Só aceita entrada `BUY` e pendentes `SELL`** | **Tipo recomendado para entradas** |

Por que **OPOCO** é o encaixe ideal para o R4:

- Uma única requisição cria a entrada **e** a proteção. Se o agente cair logo depois de enviar a ordem, a Binance coloca o par TP/SL sozinha assim que a compra executar.
- Resolve o problema clássico de a quantidade da ordem de saída não bater com o saldo por causa da comissão cobrada no ativo base. A Binance ajusta a quantidade ao `LOT_SIZE` e devolve a sobra.
- Requer as flags `otoAllowed`, `opoAllowed` e `ocoAllowed` no símbolo. Em 25/09/2026, **100% dos pares USDT, USDC, BRL e FDUSD em negociação** tinham essas três flags e também `allowTrailingStop`.

### 2.2 Trailing stop nativo (proteção que “anda” com o agente desligado)

- O parâmetro `trailingDelta` é expresso em **BIPS** (100 = 1%) e é aceito por `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT` e `TAKE_PROFIT_LIMIT`, inclusive como pernas de OCO/OTOCO/OPOCO (`pendingAboveTrailingDelta` / `pendingBelowTrailingDelta`).
- Em ordens de **venda**, o gatilho é uma queda de X bips a partir do **maior preço** negociado desde que o rastreamento começou.
- Com `stopPrice` informado, o rastreamento só começa quando o preço atinge `stopPrice`. Isso cria o **“trailing take-profit”**: um `TAKE_PROFIT` de venda com `stopPrice` = +4% e `trailingDelta` = 150 só passa a seguir o preço depois de +4% e vende quando o preço recua 1,5% do topo.
- Limites no BTCUSDT: filtro `TRAILING_DELTA` entre 10 e 2000 bips (0,1% a 20%). Os limites variam por símbolo, então é preciso ler o `exchangeInfo`.

> Com isso, uma posição pode ter **stop fixo embaixo** e **realização de lucro com trailing em cima**, tudo no servidor da Binance, e o lucro continua sendo maximizado mesmo com o agente desligado.

### 2.3 Limites e filtros relevantes

Valores do BTCUSDT em 25/09/2026. Eles variam por símbolo e devem ser lidos do `exchangeInfo`.

| Filtro / limite | Valor | Impacto |
|-----------------|-------|---------|
| `NOTIONAL.minNotional` | 5 USDT | Tamanho mínimo de ordem. Posições pequenas não conseguem ser divididas |
| `MAX_NUM_ALGO_ORDERS` | 5 por símbolo | Ordens `STOP_*`/`TAKE_PROFIT_*` contam aqui. Cada posição protegida usa 1–2. Limita *pyramiding* |
| `MAX_NUM_ORDER_LISTS` | 20 por símbolo | Limite de listas abertas |
| `MAX_NUM_ORDERS` | 200 por símbolo | — |
| `PERCENT_PRICE_BY_SIDE` | faixa por lado | Preços-limite muito distantes são rejeitados |
| Rate limits | `REQUEST_WEIGHT` 6000/min; `ORDERS` 100/10s e 200.000/dia; `RAW_REQUESTS` 300.000/5min | Desde 02/04/2026, os endpoints de ordem **bem-sucedidos têm peso 0**. Consultas continuam pesando |
| *Price Range Execution Rule* | faixa em torno de um preço de referência | Em movimentos extremos, uma ordem tomadora pode **expirar** com `expiryReason = EXECUTION_RULE_PRICE_RANGE_EXCEEDED`, e isso inclui um stop disparado. O agente precisa detectar esse caso e re-proteger |

Outros pontos da API:

- **Alterar ordem:** `POST /api/v3/order/cancelReplace` cancela e recria de forma atômica, mas vale **só para ordens simples**. `PUT /api/v3/order/amend/keepPriority` só **reduz quantidade**. **Não existe substituição atômica de uma *order list***. Para mover o stop de um OCO é preciso cancelar a lista e criar outra, o que deixa uma janela curta sem proteção. O desenho trata essa janela (ver doc 03).
- **User Data Stream:** o modelo antigo com `listenKey` em `stream.binance.com` foi depreciado em abril/2025 e removido da documentação em outubro/2025. O caminho atual é a **WebSocket API** (`userDataStream.subscribe` ou `userDataStream.subscribe.signature`, que funciona com qualquer tipo de chave).
- **Evento `serverShutdown`:** os servidores WebSocket avisam antes de desconectar. O cliente deve reconectar de forma proativa.
- **Consulta de comissões:** `GET /api/v3/account/commission` retorna as taxas reais da conta, que devem entrar no cálculo de risco/retorno.
- **Delistagens:** `GET /sapi/v1/spot/delist-schedule` lista os pares com delistagem agendada. É o mesmo endpoint que o `DelistFilter` do Freqtrade usa.

### 2.4 Ambientes de teste

| Ambiente | URL | Quando usar |
|----------|-----|-------------|
| **Spot Testnet** | `testnet.binance.vision` | Testes de integração da API (tipos de ordem, erros, filtros). Liquidez e preços **não** são realistas |
| **Demo Mode** (disponível desde 29/01/2026) | REST `demo-api.binance.com`, WS API `demo-ws-api.binance.com` | **Paper trading realista** com a mesma API. Substitui a necessidade de escrever um simulador próprio de *dry-run* |

### 2.5 Segurança e elegibilidade da conta

- **Chave de API:** prefira **Ed25519** (recomendação oficial da Binance). Permissões apenas de *Reading* e *Spot Trading*. **Saques desabilitados**. **Lista de IPs restrita** ao IP fixo do servidor.
- **Local restrito:** a Binance responde **HTTP 451** para IPs de locais restritos (ex.: EUA e Ontário), o que inclui regiões de nuvem desses países. O servidor precisa ficar numa região permitida (ex.: São Paulo, Frankfurt, Tóquio).
- **Subcontas:** estão disponíveis apenas para contas corporativas ou pessoas físicas **VIP 1+**. Para uma conta comum, vários perfis rodando ao mesmo tempo precisam dividir a mesma conta com um **razão virtual** (*virtual ledger*) de capital por perfil. Recomenda-se uma **conta dedicada ao agente**, separada dos investimentos pessoais.

### 2.6 SDKs e ferramentas oficiais

| Ferramenta | O que é | Avaliação |
|------------|---------|-----------|
| **`binance-sdk-spot`** (PyPI) | SDK Python oficial e modular (Python ≥ 3.10) com REST, WebSocket API e Streams, e reconexão automática | **Recomendado** para o núcleo de execução |
| **CCXT** | Biblioteca multi-exchange | Alternativa madura. Order lists da Binance só pela API “implícita”, sem tipagem. Útil se um dia houver outras exchanges |
| **Binance Skills Hub** (`binance/binance-skills-hub`, MIT) | *Agent Skills* (`SKILL.md`) sobre o `binance-cli`, para agentes LLM (Claude Code, OpenClaw, LangChain etc.) | Bom para **exploração e pesquisa interativa** durante o desenvolvimento. **Não é indicado para o caminho crítico de execução**: o próprio skill instrui o agente a pedir que o usuário digite `CONFIRM` em transações de produção, porque foi desenhado para ter um humano no circuito |

## 3. Panorama de projetos open source avaliados

| Projeto | O que resolve bem | Limitações para os nossos requisitos | Licença |
|---------|-------------------|--------------------------------------|---------|
| **Freqtrade** (releases mensais, 2026.x) | Infraestrutura completa: conexão, *dry-run*, backtest, hyperopt, FreqUI, Telegram, API REST, persistência SQLite/Postgres, *Protections* (StoplossGuard, MaxDrawdown, LowProfitPairs, CooldownPeriod), *pairlists* dinâmicas incluindo **RemotePairList** (lê JSON de URL ou arquivo), FreqAI (ML) | **R4 parcial:** na Binance Spot, o `stoploss_on_exchange` é só *stop-loss-limit*. O trailing no servidor é feito pelo bot cancelando e recriando a ordem a cada `stoploss_on_exchange_interval` (padrão 60 s), então **congela com o bot desligado**. **Não coloca take-profit na exchange** (pedido aberto na issue #7499). Não usa OCO/OTOCO/OPOCO | GPL-3.0 |
| **Hummingbot** + **Hummingbot API** + **Condor** | Executores V2 com *triple barrier* (TP/SL/tempo/trailing); API de orquestração; **Condor** (lançado em abril/2026) é um *harness* de agentes LLM com separação entre camada “agêntica” e camada determinística e um *risk engine* | As barreiras são **monitoradas pelo executor em processo**, portanto o bot precisa estar ligado. O deploy tem várias peças (API FastAPI + PostgreSQL + broker EMQX + Condor + dashboard). Foco histórico em *market making*. Condor ainda está em “desenvolvimento ativo” | Apache-2.0 |
| **OctoBot** | Bot pronto com UI, estratégias de grid/DCA, conectores de IA (OpenAI/Ollama) e indicadores sociais | Menos flexível para lógica sob medida. Mesmo problema de R4 | GPL-3.0 |
| **Jesse** | Backtest excelente e sintaxe simples | O módulo de live trading tem outro modelo de distribuição (verificar); pouco ganho sobre as outras opções | MIT (núcleo) |
| **NautilusTrader** | Motor de alto desempenho, mesmo código em backtest e live, ordens contingentes | Curva de aprendizado alta; exagero para swing trading de baixa frequência (contra o R8) | LGPL-3.0 |
| **TradingAgents** (TauricResearch, v0.3.x em 2026) | Framework multiagente em **LangGraph** (analistas técnico, de sentimento e de notícias, debate bull/bear, gestor de risco), com suporte a Claude, GPT, Gemini etc. | É um **framework de pesquisa e decisão**, não de execução. Custo alto de tokens por decisão | Apache-2.0 |
| **LangGraph** (insight 1) | Grafos de estado com *checkpointers* para agentes | Útil quando o LLM conduz um fluxo com ciclos. No desenho recomendado, o fluxo é um **pipeline determinístico** e o LLM é **um passo** com ferramentas, então LangGraph vira dependência sem ganho | MIT |

**Conclusão da pesquisa:** nenhum framework maduro usa hoje as *order lists* nativas da Binance (OPOCO/OTOCO com trailing) para manter a proteção **independente do bot**. Esse é o requisito que mais pesa na escolha da arquitetura.

## 4. Fontes de dados para análise técnica e de sentimento

| Fonte | Tipo | Custo | Uso proposto |
|-------|------|-------|--------------|
| Binance Spot (klines, tickers 24h, *order book*) | Técnica | Grátis | OHLCV, volume, spread, profundidade (checagem de *slippage*) |
| Binance Futures, endpoints públicos (*funding rate*, *open interest*, *long/short ratio*) | Sentimento quantitativo | Grátis | Medir alavancagem e euforia do mercado (sinal de regime) |
| Fear & Greed Index (alternative.me; também no plano gratuito da CoinMarketCap) | Sentimento agregado | Grátis | Regime de mercado e disjuntor em extremos |
| RSS de veículos cripto (CoinDesk, Cointelegraph, Decrypt, The Block etc.) | Notícias | Grátis | Insumo principal para o analista LLM |
| Anúncios da Binance (listagens, delistagens, *monitoring tag*) e `delist-schedule` | Eventos | Grátis | Veto automático e oportunidade |
| CoinGecko (plano Demo) | Market cap, categorias, *trending* | Grátis com limite | Classificação de ativos em *tiers* (large/mid/small cap) |
| **Busca web do LLM** (ferramenta `web_search` da API Claude) | Notícias sob demanda | US$ 10 por 1.000 buscas + tokens | Verificar catalisadores e confirmar notícias críticas |
| CryptoPanic, Santiment, LunarCrush, Messari | Agregadores e social | Pagos (segundo fontes secundárias, a API gratuita do CryptoPanic foi descontinuada em 2026, confirmar) | Opcional, numa evolução futura |

## 5. Armadilhas conhecidas (e como o desenho as evita)

| Armadilha | Consequência | Mitigação no desenho |
|-----------|--------------|----------------------|
| LLM decidindo e executando ordens diretamente | Não determinismo, alucinação de preço/quantidade, custo alto | O LLM **só produz dados estruturados** (scores e vetos). Quem executa é o código. O LLM **pode reduzir ou vetar risco, nunca ampliar** |
| *Prompt injection* via notícias ou redes sociais | Texto malicioso manipula a decisão | Conteúdo externo tratado como dado; saída validada por schema com limites numéricos; sem ferramentas de escrita no contexto do LLM |
| Backtest com LLM (*look-ahead*) | O modelo “conhece o futuro” de datas passadas, então o resultado não vale | Backtest só da parte técnica. O valor do LLM é medido em **forward test** (Demo Mode) com A/B entre TA pura e TA + LLM |
| Taxas ignoradas | Estratégia lucrativa no papel e perdedora na prática | Taxa real consultada via API e embutida no cálculo de R:R e nos backtests |
| Execução parcial da entrada | Parte da posição fica **sem proteção**, porque o OPOCO só arma o TP/SL após execução total | Entrada `LIMIT` com `FOK`, ou tratamento explícito de parcial (ver doc 03) |
| Stop-limit “pulado” em gap | O stop dispara mas não executa | Perna de baixo como `STOP_LOSS` (a mercado) por padrão; monitorar `expiryReason` |
| Delistagem ou *depeg* de stablecoin | Perda abrupta ou ativo preso | Filtro de delistagem, exclusão de ativos “monitorados”, disjuntor de *depeg* da moeda de cotação |
| Relógio dessincronizado | Rejeição por `timestamp` e `recvWindow` | NTP no host e checagem periódica de `serverTime` |
| Duas instâncias rodando | Ordens duplicadas | *Lock* exclusivo no banco (*advisory lock*) e `clientOrderId` determinístico |
