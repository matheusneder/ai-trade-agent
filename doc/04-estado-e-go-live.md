# 04 — Estado atual e *go-live*

> Situação em 03/10/2026: o que já foi validado, o que falta antes de operar capital real, como o projeto é testado, quanto custa e os riscos conhecidos.

## 1. Estado

Todas as partes da arquitetura (doc 03) estão implementadas: exchange e execução, persistência e recuperação, universo, sinais, perfis e laboratório, analista LLM, risco e Telegram, e observabilidade.

O agente roda em **paper trading no Demo Mode** desde 29/09/2026, com a trava de ordens ligada (`TA_TRADING_ENABLED=true`): as ordens são reais no Demo. A máquina é um Windows com Rancher Desktop, ainda não uma VPS. Os perfis em uso são `swing_trend` e `momentum_alpha` (doc 03, §8).

| Validação | Resultado |
|-----------|-----------|
| Tipos de ordem (spike) | 19/19 no Spot Testnet, 28/09 (doc 01, §3) |
| Teste *live* (abrir, armar, ajustar, fechar) | aprovado no Spot Testnet |
| OPOCO no Demo | em operação desde 29/09; a quantidade protegida é a recebida depois da comissão (doc 01, §3) |
| Testes de caos (queda depois da intenção e antes do envio, depois do envio e antes do registro, durante um ajuste; saída e proteção expirada com o agente fora; reinícios repetidos) | aprovados na suíte de integração (`tests/integration/test_chaos.py`) |
| Laboratório | `swing_trend` e `momentum_alpha` com expectativa positiva fora da amostra, abaixo do *buy & hold* do BTC ([lab-resultados.md](lab-resultados.md)) |
| Avaliação do analista | 31/31 casos, 100% das saídas válidas no schema, 28/09 (doc 03, §7.5) |
| Alertas no Telegram | em uso: mudanças de estado do risco, entradas e saídas, e as 3 regras do Grafana |

**Demo até 03/10:** 5 posições. Quatro encerraram no stop: duas do perfil antigo `conservador` (MOVRUSDT, 01/10) e duas do `momentum_alpha` (GTCUSDT, 02/10). Uma está aberta e protegida (SUPERUSDT, `momentum_alpha`, desde 03/10). As 4 perdas seguidas acionaram a pausa global de 12h, que levou à D-026.

## 2. Checklist de *go-live*

- [ ] **4 semanas de Demo** (até cerca de 27/10/2026) sem **nenhuma** ordem duplicada e sem posição `unprotected` por mais de 30 s.
- [ ] Reconciliação sem divergências não explicadas.
- [ ] *Drawdown* máximo e perda diária dentro dos limites de cada perfil.
- [ ] Resultado ajustado ao risco, depois das taxas, comparado ao BTC *buy & hold* e a um clone "TA pura" de cada perfil (`llm.weight = 0`), com uma decisão explícita sobre o peso do LLM. **O clone ainda não existe.**
- [ ] Todos os disjuntores testados; `/halt` e `/flatten` testados no celular.
- [ ] Heartbeat externo configurado (`TA_HEALTHCHECK_URL`, hoje vazio) e testado com a máquina desligada.
- [ ] Agente numa VPS em região permitida, com pelo menos um reinício com posições abertas.
- [ ] Chave de produção com saque desabilitado, IP restrito e somente Spot.
- [ ] Backup automatizado, criptografado e fora da máquina, com restauração testada (hoje só o `pg_dump` manual do runbook).
- [ ] Runbook revisado.

Depois do *go-live*: começar com um perfil e capital pequeno (o valor que se aceita perder integralmente) e aumentar em degraus, só depois de semanas dentro dos limites de risco em cada degrau.

## 3. Estratégia de testes

| Camada | O que testa | Como |
|--------|-------------|------|
| Unitários | Arredondamento e filtros, dimensionamento, montagem de OPOCO e OCO, máquina de estados, condições de parada, schema do `MarketView` | `pytest`, com testes de propriedade (Hypothesis): "nenhuma ordem viola filtro", "risco ≤ limite" |
| Integração | Fluxos completos de execução, recuperação, risco, decisão, Telegram e analista | Binance simulada em memória (D-005), PostgreSQL num contêiner, Bot API e API Claude simuladas |
| Caos | Recuperação e idempotência | O processo morre em pontos críticos (depois da intenção, depois do envio, durante um ajuste), a posição muda com o agente fora, e a partida se repete. Falhas de rede, relógio, 418/429, JSON inválido do LLM, parcial e segunda instância têm testes próprios nas camadas acima |
| Implantação | Compose, Grafana, Loki e Alloy, Jaeger, SigNoz | Pipelines e consultas reais em contêineres; geradores de dashboards comparados com os JSON versionados |
| *Live* | Tipos de ordem e o ciclo de uma posição | `pytest -m live`, contra o Testnet ou o Demo |
| Laboratório | A parte técnica (setups e parâmetros por perfil) | Freqtrade, *walk-forward* com taxas (D-017) |
| Avaliação do LLM | Qualidade e segurança do `MarketView` | 31 casos rotulados (D-021) |
| *Forward test* | O sistema completo | Demo Mode (em andamento) |

Cobertura de 100% das linhas e ramos de `src/` (D-004), com `ruff` e `mypy` limpos.

## 4. Custos (mensal)

| Item | Valor | Observação |
|------|-------|------------|
| API Claude | US$ 15–130 | Observado no Demo: de US$ 0,52 a 4,25 por dia (29/09 a 03/10). Teto de US$ 5/dia (`config/research.yaml`) |
| VPS | US$ 20–40 | Ainda não contratada; com o SigNoz, pede pelo menos 8 GB de RAM (doc 03, §14) |
| Healthchecks.io, Grafana, Loki, Jaeger, SigNoz, Postgres | US$ 0 | Plano gratuito e software livre |
| Taxas de negociação | Variável | Tipicamente 0,1% por lado em conta comum. **É o principal custo de uma estratégia ativa** |

## 5. Riscos

| Risco | Prob. | Impacto | Mitigação |
|-------|-------|---------|-----------|
| Estratégia sem vantagem real depois das taxas | Alta | Alto | *Walk-forward* fora da amostra, Demo antes de produção, *go-live* gradual; "não operar" é um resultado válido |
| *Overfitting* no *hyperopt* | Alta | Alto | *Walk-forward*, mediana dos treinos e validação fora da amostra (os parâmetros de entrada oscilam muito entre os treinos; [lab-resultados.md](lab-resultados.md)) |
| Posição desprotegida (parcial, janela de ajuste, *price range*) | Baixa | Alto | FOK nos dois perfis, *fail-safe* de venda, alerta "Posição sem proteção" do Grafana, reconciliação a cada 5 min |
| LLM alucinando ou manipulado por *prompt injection* | Média | Médio | Autoridade assimétrica, schema, exigência de fontes, peso limitado e degradação segura |
| Mudanças na API da Binance | Média | Médio | Cliente próprio com superfície pequena, acompanhamento do changelog e testes *live* |
| Custo do LLM acima do previsto | Baixa | Baixo | Teto diário, *prompt caching* e modelos configuráveis |
| Restrição regulatória ou de localização | Baixa | Alto | VPS em região permitida e conta verificada |
| Obrigações fiscais | Certa | Médio | Exportação do diário de operações (`fills`, custos, PnL) para apuração. Consultar um contador |

## 6. Decisões em aberto

1. **VPS:** provedor e região.
2. **Conta de produção:** conta dedicada e capital inicial.
3. **Peso do LLM:** depende do A/B "TA pura" × "TA + LLM", que ainda precisa ser implementado.
4. **Canal de alertas:** só Telegram, ou também e-mail (via Healthchecks.io)?
5. **Observabilidade em produção:** manter o Jaeger, o Loki e o SigNoz em paralelo, ou ficar com um só (o SigNoz é o mais pesado)?

## 7. Evoluções possíveis

- Clone "TA pura" de cada perfil para o A/B, e correlação entre o sentimento do analista e o retorno dos dias seguintes.
- Gatilhos extras de pesquisa: movimento forte do BTC, rajada de notícias sobre um ativo em carteira, ou leitura antiga antes de uma entrada.
- Relatório diário automático no Telegram e backup agendado.
- Setup de reversão à média para regime lateral.
- Modelo de *ranking* com ML (ex.: LightGBM) treinado offline, como mais um score.
- Pesquisa multiagente com debate *bull/bear*, só se o A/B mostrar ganho do analista único.
