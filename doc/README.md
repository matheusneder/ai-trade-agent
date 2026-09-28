# AI Trade Agent — Documentação de arquitetura

Agente autônomo de trade de criptomoedas na **Binance (Spot)**, com decisões baseadas em análise técnica e em pesquisa de mercado (notícias e sentimento), perfis de risco parametrizáveis, proteção de posições na própria exchange e recuperação automática após falhas.

## Resumo executivo

Foram avaliadas **três arquiteturas**:

| Proposta | Ideia central | Veredito |
|----------|---------------|----------|
| **A — Freqtrade + Analista LLM** | Framework maduro para execução e TA; o LLM alimenta pares e contexto via JSON | Mais rápida de construir, mas **só protege com stop fixo** quando o bot está desligado (sem take-profit nem trailing no servidor) |
| **B — Núcleo enxuto com proteção nativa** ⭐ | Serviço Python próprio e pequeno; toda posição nasce como **OPOCO** na Binance (compra → OCO com **trailing take-profit** + **stop-loss** no servidor) | **Recomendada**: única que atende por completo à proteção com o agente desligado, com poucas peças |
| **C — Agente LLM-cêntrico** (Condor/Hummingbot ou LangGraph + MCP) | O LLM decide e age em cada ciclo | Mais “agêntica”, porém não determinística, cara, difícil de testar e **sem proteção no servidor** |

**Por que a Proposta B:**

1. Em 2026, a Binance Spot oferece **OPOCO** (a quantidade da saída é a efetivamente recebida na compra) e **trailing stop nativo** (`trailingDelta`) em 100% dos pares USDT, USDC e BRL. Assim, a Binance protege a posição e realiza lucro **mesmo com o agente desligado**.
2. Com a parte crítica guardada na exchange, a **recuperação após falha** vira “reconciliar e seguir”.
3. O **LLM atua como analista**, com saída estruturada e autoridade assimétrica: pode vetar ou reduzir risco, nunca ampliar.
4. **3 contêineres** (agente, PostgreSQL, Grafana) + Telegram + heartbeat externo. Freqtrade como laboratório offline de backtest e **Demo Mode da Binance** como *paper trading*, sem reinventar backtester nem simulador.

**Esforço estimado:** cerca de 11 semanas de construção (1 dev em tempo integral) + no mínimo 4 semanas de *paper trading* antes de capital real.

## Documentos

| # | Documento | Conteúdo |
|---|-----------|----------|
| 01 | [Contexto e pesquisa](01-contexto-e-pesquisa.md) | Leitura dos requisitos, recursos da API Binance (order lists, trailing, limites, ambientes, segurança), panorama de projetos open source, fontes de dados, armadilhas |
| 02 | [Propostas de arquitetura](02-propostas-de-arquitetura.md) | Propostas A, B e C com diagramas, atendimento por requisito, matriz comparativa e recomendação |
| 03 | [Arquitetura recomendada](03-arquitetura-recomendada.md) | Detalhamento da Proposta B: stack, módulos, ciclo de decisão, analista LLM, perfis, OPOCO e ciclo de vida da posição, disjuntores, persistência e recuperação, monitoramento, segurança, implantação |
| 04 | [Plano de construção](04-plano-de-construcao.md) | Fases com critérios de saída, cronograma, estratégia de testes, checklist de *go-live*, *runbook*, custos, riscos, decisões em aberto |
| 05 | [Referências](05-referencias.md) | Links para documentação e projetos consultados |
| — | [Spike OPOCO](spike-opoco.md) | Resultados da Fase 0 no Spot Testnet (19/19) e achados sobre a API |
| — | [Runbook](runbook.md) | Operação: subir e verificar, incidentes, comandos do Telegram, manutenção |
| — | [Resultados do laboratório](lab-resultados.md) | *Walk-forward* da Fase 3: baselines, diagnóstico, otimização fora da amostra e conclusão por perfil |
| — | [Insights iniciais](insights-iniciais.md) | Documento de partida (Gemini), considerado na análise |

## Aviso

Este material descreve **arquitetura de software**. Os parâmetros numéricos (percentuais de risco, stops, pesos) são exemplos para ilustrar a configuração e **não constituem recomendação de investimento**. Operar criptoativos envolve risco de perda total do capital. Valide tudo em backtest e *paper trading* e só use capital que você aceita perder.
