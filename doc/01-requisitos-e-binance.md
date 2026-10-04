# 01 — Requisitos e a API da Binance

> O que o agente precisa garantir e os fatos da API Spot da Binance de que o desenho depende. Os fatos da API foram conferidos em 25/09/2026 na documentação oficial (`binance-spot-api-docs`) e no endpoint público `exchangeInfo`. O spike de 28/09 os confirmou no Spot Testnet, e a operação no Demo Mode confirma desde 29/09 (§3). Links em [05-referencias.md](05-referencias.md).

## 1. Requisitos

| # | Requisito | Como o agente atende |
|---|-----------|----------------------|
| R1 | Escolher pares e comprar ou vender sem intervenção | O universo é montado a cada ciclo, com filtros de liquidez, spread, histórico, delistagem e *tiers*. Depois vêm os sinais técnicos, o analista LLM e a carteira de cada perfil, até o OPOCO (doc 03, §6) |
| R2 | Perfis de alocação | Perfis declarativos em `config/profiles.yaml`, validados por schema. Hoje são `swing_trend` e `momentum_alpha`, ambos em 4h (doc 03, §8) |
| R3 | Decisões com análise técnica, notícias e sentimento | A análise técnica são funções puras (`signals/`). Notícias e sentimento passam pelo analista LLM, com saída estruturada e limitada por código (doc 03, §7) |
| R4 | Proteção mesmo com o agente desligado | Toda entrada é um OPOCO: a Binance arma o take-profit com trailing e o stop no próprio servidor (§2; doc 03, §9) |
| R5 | Condições de parada parametrizáveis | `config/stop_conditions.yaml`, com estados persistidos `running`, `paused`, `halted` e `flattening` (doc 03, §10) |
| R6 | Monitoramento, alertas e painéis | Telegram para alertas e comandos, Grafana, Loki, Jaeger e SigNoz, e heartbeat externo opcional (doc 03, §12) |
| R7 | Resiliência e recuperação automática | A exchange é a fonte da verdade. As intenções são gravadas antes do envio, os IDs são determinísticos, e a reconciliação roda na partida e a cada 5 min (doc 03, §11) |
| R8 | Simplicidade e componentes maduros | O núcleo é um processo Python com PostgreSQL. A observabilidade roda em contêineres à parte, e o agente funciona sem ela (doc 03, §3) |

**Premissas:**

- Mercado **Spot sem alavancagem**: só existe posição comprada, e "vender" é voltar para a moeda de cotação.
- **Swing trading** em candles de 4h. Não é HFT nem scalping: as taxas e a latência do LLM inviabilizam frequências altas.
- Moeda de cotação **USDT** (D-013).
- O agente opera até `managed_capital` e ignora os saldos que não são dele. Mesmo assim, recomenda-se uma conta dedicada.

## 2. O que a Binance oferece

### 2.1 Ordens condicionais nativas (*order lists*)

O endpoint antigo `POST /api/v3/order/oco` está depreciado. O atual é `POST /api/v3/orderList/oco`.

| Tipo | Endpoint REST | Como funciona | Uso no agente |
|------|---------------|---------------|---------------|
| **OCO** | `POST /api/v3/orderList/oco` | Duas ordens, uma acima e outra abaixo do preço; quando uma executa, a outra é cancelada. `aboveType`: `STOP_LOSS_LIMIT`, `STOP_LOSS`, `LIMIT_MAKER`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`. `belowType`: `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`. Aceita `aboveTrailingDelta` e `belowTrailingDelta` | Re-proteção e ajuste de uma posição aberta |
| **OTO** | `POST /api/v3/orderList/oto` | Ordem de trabalho (`LIMIT`/`LIMIT_MAKER`) que, ao executar **totalmente**, dispara uma ordem pendente | Não usado |
| **OTOCO** | `POST /api/v3/orderList/otoco` | Ordem de entrada que, ao executar totalmente, coloca um **par OCO** | Não usado (o OPOCO o substitui) |
| **OPO** | `POST /api/v3/orderList/opo` | Igual ao OTO, mas a ordem pendente usa **a quantidade efetivamente recebida** na entrada, já descontada a comissão | Não usado |
| **OPOCO** | `POST /api/v3/orderList/opoco` | Igual ao OTOCO, com a quantidade recebida. **Só aceita entrada `BUY` e pendentes `SELL`** | **Toda entrada** |

Por que o OPOCO atende ao R4:

- Uma única requisição cria a entrada **e** a proteção. Se o agente cair logo depois de enviar a ordem, a Binance coloca o par TP/SL sozinha assim que a compra executar.
- A quantidade da saída é a recebida, depois da comissão cobrada no ativo base. A Binance a ajusta ao `LOT_SIZE`.
- Exige as flags `otoAllowed`, `opoAllowed` e `ocoAllowed` no símbolo, além de `allowTrailingStop` para o trailing. Em 25/09/2026, 100% dos pares USDT em negociação tinham as quatro. O universo descarta os que não têm (doc 03, §6.1).

### 2.2 Trailing stop nativo (proteção que "anda" com o agente desligado)

- O parâmetro `trailingDelta` é expresso em **bips** (100 = 1%) e é aceito por `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT` e `TAKE_PROFIT_LIMIT`, inclusive como pernas de OCO e OPOCO (`pendingAboveTrailingDelta`/`pendingBelowTrailingDelta`).
- Em ordens de **venda**, o gatilho é uma queda de X bips a partir do **maior preço** negociado desde que o rastreamento começou.
- Com `stopPrice` informado, o rastreamento só começa quando o preço atinge o `stopPrice`. Isso cria o **trailing take-profit** que o agente usa: um `TAKE_PROFIT` de venda com `stopPrice` = +9,7% e `trailingDelta` = 100 só passa a seguir o preço depois de +9,7% e vende quando o preço recua 1% do topo.
- O filtro `TRAILING_DELTA` limita o valor: no BTCUSDT, entre 10 e 2000 bips (0,1% a 20%). Como os limites variam por símbolo, o agente valida cada ordem contra o `exchangeInfo`.

### 2.3 Limites e filtros relevantes

Valores do BTCUSDT em 25/09/2026. Eles variam por símbolo, e o agente os lê do `exchangeInfo`.

| Filtro ou limite | Valor | Impacto |
|------------------|-------|---------|
| `NOTIONAL.minNotional` | 5 USDT | Tamanho mínimo de ordem |
| `MAX_NUM_ALGO_ORDERS` | 5 por símbolo | Ordens `STOP_*`/`TAKE_PROFIT_*` contam aqui. Cada posição protegida usa 2. O agente abre no máximo uma posição por ativo |
| `MAX_NUM_ORDER_LISTS` | 20 por símbolo | Limite de listas abertas |
| `PERCENT_PRICE_BY_SIDE` | faixa por lado | Preços-limite muito distantes são rejeitados |
| Rate limits | `REQUEST_WEIGHT` 6000/min; `ORDERS` 100/10s e 200.000/dia; `RAW_REQUESTS` 300.000/5min | Desde 02/04/2026, os envios de ordem bem-sucedidos têm peso 0. As consultas continuam pesando (painel "Peso usado (1 min)") |
| *Price Range Execution Rule* | faixa em torno de um preço de referência | Em movimentos extremos, uma ordem tomadora pode **expirar** com `expiryReason = EXECUTION_RULE_PRICE_RANGE_EXCEEDED`, inclusive um stop disparado. A posição vai para `UNPROTECTED`, e o agente re-protege ou vende (D-010) |

Outros pontos da API:

- **Não existe substituição atômica de uma *order list*.** `cancelReplace` vale só para ordens simples, e `amend/keepPriority` só reduz quantidade. Para mover o stop de um OCO, o agente cancela a lista e cria outra, com *fail-safe* de venda se o novo OCO for rejeitado (D-007).
- **User Data Stream:** o modelo com `listenKey` foi depreciado em abril de 2025. O agente usa a **WebSocket API** (`userDataStream.subscribe.signature`) e reconecta de forma proativa ao receber o evento `serverShutdown`.
- **Comissões:** `GET /api/v3/account/commission` retorna as taxas da conta. O agente mede a comissão de cada execução pelos `fills`, e a validação pré-ordem usa a taxa de ida e volta configurada (`pre_trade.round_trip_fee_pct`, 0,2%).
- **Delistagens:** `GET /sapi/v1/spot/delist-schedule` lista os pares com delistagem agendada, que o universo exclui. A rota só existe na produção: a Testnet e o Demo Mode não têm as rotas `/sapi` (HTTP 404), e nelas o agente não a consulta.

### 2.4 Ambientes

| Ambiente | URL | Uso |
|----------|-----|-----|
| **Spot Testnet** | `testnet.binance.vision` | Validar tipos de ordem, erros e filtros (spike e `pytest -m live`). Os preços e a liquidez não são realistas, e o histórico é curto (cerca de 20 dias): os sinais precisam de 201 candles e o universo de 30 dias, então o ciclo de decisão não roda ali |
| **Demo Mode** | REST `demo-api.binance.com`, WS API `demo-ws-api.binance.com` | *Paper trading* com dados reais de mercado e a mesma API. É onde o agente roda hoje (`TA_BINANCE_ENV=demo`) |
| **Produção** | `api.binance.com` | Ainda não usada. As ordens exigem `--confirm-prod` na CLI (doc 04) |

### 2.5 Segurança e elegibilidade da conta

- **Chave de API:** **Ed25519** (recomendação da Binance), só com *Reading* e *Spot Trading*, **saques desabilitados** e lista de IPs restrita ao IP fixo do servidor.
- **Local restrito:** a Binance responde **HTTP 451** para IPs de locais restritos (ex.: EUA e Ontário), o que inclui regiões de nuvem desses países. O servidor precisa ficar numa região permitida (ex.: São Paulo, Frankfurt, Tóquio).
- **Subcontas:** só para contas corporativas ou pessoas físicas **VIP 1+**. Por isso, os perfis dividem a mesma conta: cada um tem uma cota do `managed_capital`, e as posições são identificadas pelo prefixo dos IDs de ordem (doc 03, §8).

## 3. Comportamentos confirmados

**Spot Testnet, 28/09/2026** (`scripts/spike_opoco.py`, BTCUSDT): 19 de 19 verificações aprovadas. Foram testados o OPOCO com compra `LIMIT FOK`, `TAKE_PROFIT` com ativação e trailing e `STOP_LOSS` fixo, a variante com `STOP_LOSS` só com trailing, o FOK não executável (entrada `EXPIRED`, pernas não armadas), o OCO avulso com consulta e cancelamento, e o User Data Stream pela WebSocket API. O teste *live* (abrir, armar, ajustar e fechar) também passou no Testnet.

**Demo Mode, desde 29/09/2026:** o agente abre e protege posições reais de *paper trading* com OPOCO. A quantidade protegida é a recebida depois da comissão, arredondada para baixo ao `LOT_SIZE` (ex.: 25,10487 recebidos e 25,104 protegidos). Isso confirma a semântica do OPO com comissão cobrada no ativo base.

Achados que o código trata:

1. **Pernas pendentes sem `origQty`:** na resposta do `orderList/opoco`, o TP e o SL vêm `PENDING_NEW` e sem quantidade, que só é definida quando a entrada executa. Nas pernas pendentes, `origQty` ausente vale 0.
2. **As pernas continuam `PENDING_NEW` por alguns instantes** depois da execução da entrada. A sincronização reconhece esse intervalo (veredito `ARMING`, "OCO sendo armado") e consulta de novo.
3. **O `listClientOrderId` pode ser reutilizado depois que a lista termina.** Por isso o `seq` dos IDs sempre avança, e um resultado incerto leva a uma consulta, nunca a um reenvio (D-006, D-012).
4. **`contingencyType` volta como `"OTO"` numa lista OPOCO.** É só informativo: nenhuma lógica depende desse campo.
5. **O saldo recebido fica travado pelo OCO** (`locked`, não `free`). O recebido é medido pelas execuções, não pelo saldo livre.

O `scripts/spike_opoco.py` repete essas verificações num ambiente novo e recusa o ambiente `prod` (README).

## 4. Fontes de dados

| Fonte | Tipo | Uso no agente |
|-------|------|---------------|
| Binance Spot (klines, tickers de 24h, melhor oferta) | Técnica | Sinais, universo (volume, spread) e preço de entrada |
| `delist-schedule` da Binance | Eventos | Exclusão do universo (só na produção) |
| Anúncios do site da Binance (listagens, delistagens, notícias) | Eventos | Notícias para o analista |
| RSS: CoinDesk, Cointelegraph, The Block, Decrypt | Notícias | Notícias para o analista |
| Fear & Greed Index (alternative.me) | Sentimento | Métrica para o analista e disjuntor `fear_greed_below` |
| Binance Futures, endpoints públicos (*funding rate*, *open interest*) | Sentimento quantitativo | Métricas para o analista (até 12 símbolos) |
| Busca e leitura web do Claude (`web_search`, `web_fetch`) | Notícias sob demanda | Etapa de verificação do analista, com até 5 buscas e 3 leituras por ciclo (US$ 10 por 1.000 buscas, mais os tokens) |

As fontes ficam em `config/research.yaml` (D-020), todas públicas e sem chave. Cada uma falha de forma isolada.

## 5. Armadilhas conhecidas e como o agente as evita

| Armadilha | Consequência | Mitigação |
|-----------|--------------|-----------|
| LLM decidindo e executando ordens | Não determinismo, alucinação de preço ou quantidade, custo alto | O LLM **só produz dados estruturados** (scores e vetos), e quem executa é o código. Ele **pode reduzir ou vetar risco, nunca ampliar** |
| *Prompt injection* via notícias | Texto malicioso manipula a decisão | O conteúdo externo entra como dado não confiável. A saída é validada por schema e limitada por código, e o LLM não tem ferramentas de escrita nem de execução |
| Backtest com LLM (*look-ahead*) | O modelo "conhece o futuro" de datas passadas | O laboratório testa só a parte técnica. O valor do LLM precisa ser medido no Demo (o A/B ainda não existe; doc 04) |
| Taxas ignoradas | Estratégia lucrativa no papel e perdedora na prática | O laboratório cobra 0,1% por lado. A validação pré-ordem exige R:R mínimo depois da taxa de ida e volta, e o PnL vem das execuções reais (`fills`) |
| Execução parcial da entrada | Parte da posição fica sem proteção, porque o OPOCO só arma depois da execução total | Entrada `LIMIT` com `FOK` nos dois perfis. O modo `limit_maker_gtc` trata a parcial (doc 03, §9.3) |
| Stop-limit "pulado" num *gap* | O stop dispara, mas não executa | Perna de baixo `STOP_LOSS` (a mercado), com `expiryReason` monitorado |
| Delistagem ou *depeg* | Perda abrupta ou ativo preso | O universo exclui delistagens agendadas, o analista pode vetar, e o disjuntor `quote_depeg_pct` vende tudo se o USDT sair da paridade |
| Relógio dessincronizado | Rejeição por `timestamp` e `recvWindow` (`-1021`) | Sincronia na partida e a cada 10 min, com três amostras, e nova medição com uma repetição quando a Binance recusa (runbook, §2.6) |
| Duas instâncias rodando | Ordens duplicadas | *Advisory lock* exclusivo no banco e IDs de ordem determinísticos |
