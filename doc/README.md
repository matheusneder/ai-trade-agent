# AI Trade Agent — Documentação

Agente autônomo de trade de criptomoedas na **Binance Spot**. Ele decide com análise técnica e com a leitura de mercado de um analista LLM (notícias e sentimento), opera perfis de risco parametrizáveis e protege cada posição na própria exchange. Ele também se recupera sozinho depois de uma queda.

## Em resumo

- **Proteção na Binance:** toda posição nasce como **OPOCO**, uma compra que, ao executar, arma no servidor da Binance um OCO com **trailing take-profit** e **stop-loss**. A posição continua protegida e realizando lucro com o agente desligado.
- **Recuperação:** com o que é crítico guardado na exchange, uma queda se resolve na partida seguinte: o agente **reconcilia e segue**.
- **O LLM é analista, não operador:** produz uma leitura estruturada, com autoridade assimétrica. Pode vetar ou reduzir o risco, nunca ampliar.
- **Peças:** um processo Python com PostgreSQL. Ao lado ficam Telegram (alertas e comandos), Grafana, Loki, Jaeger e SigNoz (observabilidade), o Freqtrade como laboratório offline de backtest e o Demo Mode da Binance para o *paper trading*.
- **Estado:** em *paper trading* no Demo desde 29/09/2026, com os perfis `swing_trend` e `momentum_alpha` (4h). O checklist do *go-live* está no doc 04.

## Documentos

| # | Documento | Conteúdo |
|---|-----------|----------|
| 01 | [Requisitos e a API da Binance](01-requisitos-e-binance.md) | Requisitos, *order lists* e trailing nativos, limites, ambientes, segurança da conta, comportamentos confirmados no Testnet e no Demo, fontes de dados e armadilhas |
| 03 | [Arquitetura](03-arquitetura.md) | Princípios, contêineres, stack, código, tarefas, ciclo de decisão, analista LLM, perfis, OPOCO e ciclo de vida da posição, risco, persistência e recuperação, monitoramento, segurança, implantação e o registro de decisões (D-001 a D-028) |
| 04 | [Estado atual e *go-live*](04-estado-e-go-live.md) | O que já foi validado, checklist para produção, estratégia de testes, custos, riscos e decisões em aberto |
| 05 | [Referências](05-referencias.md) | Documentação da Binance, das ferramentas e das fontes de dados |
| — | [Runbook](runbook.md) | Operação: subir e verificar, logs, traces, SigNoz, incidentes, comandos do Telegram e manutenção |
| — | [Resultados do laboratório](lab-resultados.md) | *Walk-forward* que calibrou os perfis em uso |

## Aviso

Os parâmetros numéricos (percentuais de risco, stops, pesos) são a configuração do projeto e **não constituem recomendação de investimento**. Operar criptoativos envolve risco de perda total do capital. Valide tudo em backtest e *paper trading* e só use capital que você aceita perder.
