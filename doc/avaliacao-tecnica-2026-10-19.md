# Avaliação do AI Trade Agent — 19/10/2026

> Registro da avaliação inicial. As decisões posteriores do usuário e a implementação
> estão em [hardening-plan.md](hardening-plan.md) e [hardening-validation.md](hardening-validation.md).
> A duplicidade de observabilidade é intencional: ambas as stacks foram preservadas.

O projeto tem uma base de engenharia consistente para um agente de Binance Spot, mas ainda não demonstra rentabilidade líquida sustentável em produção. A melhor oportunidade é preservar o núcleo determinístico, reduzir o custo operacional e fechar as diferenças entre simulação e execução antes de buscar estratégias mais complexas.

O objetivo recomendado é **maximizar resultado líquido ajustado ao risco**, sujeito a limites explícitos de drawdown, exposição e custo. Nenhuma stack ou calibração garante lucro.

**Escopo e evidência.** Revisão do código de decisão, execução, risco, pesquisa, portfólio, configurações, laboratório, deploy e documentação. Os resultados históricos citados vêm de `doc/lab-results.md`; não foram recalculados nesta avaliação. Não consultei saldos, credenciais, faturas ou a base operacional; portanto, o estado atual da Demo e o consumo real de infraestrutura não estão comprovados aqui. Os achados de concorrência são de análise estática, não incidentes reproduzidos. Referências de código abaixo usam caminhos relativos ao repositório e linhas da versão analisada.

**Verificação executada.** Python 3.14.3; `.venv/Scripts/python.exe -m pytest tests/unit -q`: **406 testes aprovados em 13,93 segundos**, em execução fora do sandbox após restrições de acesso no ambiente isolado. Não executei testes de integração, live, smoke de dashboards, novo backtest ou medição de cobertura. Os testes existentes passarem não invalida os cenários adicionais identificados. Somente este relatório foi acrescentado; código e configuração operacional não foram alterados.

**Propósito e funcionamento.** O agente seleciona ativos líquidos, calcula sinais de tendência e rompimento em candles de 4h, consulta uma leitura de mercado por LLM, dimensiona posições por perfil e valida condições de risco. Executa entradas FOK com proteção nativa OPOCO/OCO, registra intenções e resultados no PostgreSQL e reconcilia o estado com a Binance. Telegram serve como interface operacional. Freqtrade é o laboratório offline.

O capital configurado é 1.000 USDT, dividido em `swing_trend` (65%), `momentum_alpha` (30%) e `alt_atr` (5%). Isso é configuração, não comprovação de saldo real. Os três perfis estão habilitados.

**O que preservar.** Python com asyncio em um monólito modular; PostgreSQL como diário transacional; Decimal em dinheiro e ordens; schemas Pydantic; intenções persistidas antes do envio; identificação e consulta de ordens com resultado incerto; proteção na corretora; reconciliação; bloqueio de múltiplas instâncias; sinais compartilhados com o laboratório. São escolhas adequadas ao volume e à frequência de operação. Redis, Kafka, microserviços e Kubernetes não apresentam benefício demonstrado neste escopo.

A documentação oficial confirma que OPOCO utiliza a quantidade recebida pela entrada, com regras de comissão e filtros aplicadas às ordens pendentes. Isso sustenta a escolha arquitetural, mas proteção nativa não garante execução no preço desejado nem elimina falhas durante substituições. [Binance: OPO/OPOCO](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/opo.md).

**Prioridades recomendadas.** P0: corrigir antes de operar capital real; P1: resolver antes de ampliar capital; P2: otimizar após medição. Esforço é relativo, não estimativa de prazo.

| Prioridade | Oportunidade | Efeito esperado | Esforço |
|---|---|---|---|
| P0 | Revalidar estado de risco no envio | Evitar entrada após pause/halt/flatten | Médio |
| P0 | Retirar a espera do LLM da gestão de posições | Reduzir atraso de saídas e ajustes | Médio |
| P1 | PnL econômico completo e limite real de pesquisa | Enxergar e reduzir perdas por custo | Médio |
| P1 | Observabilidade única e opcional | Menos RAM, disco e manutenção | Médio |
| P1 | Comparativo TA puro realmente isolado | Medir contribuição líquida do LLM | Médio |
| P1 | Corrigir semântica da autoridade do LLM | Alinhar comportamento e promessa de segurança | Pequeno/médio |
| P1 | Backtest conservador e validação futura congelada | Reduzir risco de estratégia sobreajustada | Alto |
| P1 | Risco agregado, custos e preço de saída realista | Aproximar risco nominal do econômico | Médio |
| P1 | Backup restaurável e heartbeat externo | Reduzir impacto de indisponibilidade | Pequeno/médio |
| P2 | Cache de dados, consultas e deploy reproduzível | Menos trabalho repetido e mudanças invisíveis | Médio |

**1. O estado de risco pode ficar obsoleto antes da compra — P0.**

Em `src/trade_agent/decision/engine.py:326`, `run_profile()` lê o estado uma vez. Depois coleta sinais, aguarda pesquisa e processa saídas; o mesmo estado segue para `_entries()` e `PreTradeContext` (`:344`, `:362`, `:504`). `PositionService.open_position()` e o gateway não consultam o RiskGuard.

Se `/halt`, `/pause` ou um circuit breaker atuar durante a pesquisa, o ciclo pode continuar autorizado pelo estado antigo. O lock existente serializa entradas entre perfis, mas não resolve sozinho a concorrência com alterações do estado de risco.

Recomendação: revalidar estado dentro da seção de entrada e coordenar a autorização final com pause/halt/flatten, usando versão do estado ou bloqueio compartilhado de autorização. Revalidar por ordem, incluindo saldo disponível, dados recentes e exposição. Critério: um teste interrompe a pesquisa, aplica halt, libera a pesquisa e confirma zero novas entradas; outro aplica halt entre duas ordens. Definir explicitamente o tratamento de uma ordem já em trânsito.

**2. A pesquisa atrasa a gestão das posições — P0.**

`run_profile()` aguarda `_reading()` antes de `_exits()`. A configuração permite 300 segundos por chamada de modelo e continuações de pesquisa; não é um prazo total garantido do ciclo. Stop e trailing já armados continuam na corretora, mas saídas por tempo, score e ajuste de break-even aguardam a pesquisa.

Executar primeiro a gestão determinística de posições. Consumir a última leitura válida para veto e atualizar a pesquisa em tarefa independente, com prazo total, validade explícita e política para ausência de leitura. Validar que indisponibilidade do LLM não atrasa gerenciamento nem comandos de risco.

**3. A economia da operação é o principal obstáculo — P1.**

`doc/04-status-and-go-live.md` registra estimativas mensais de US$ 15–130 para LLM e US$ 20–40 para VPS. São números históricos do projeto, não cotações atuais nem faturas verificadas. Somados, representam US$ 35–170/mês: **3,5%–17% do capital configurado**, assumindo 1 USDT ≈ US$ 1, apenas para cobrir esses custos e antes de outros custos operacionais. A taxa necessária sobre o capital efetivamente investido pode ser maior, pois há reservas e limites.

O orçamento de pesquisa de US$ 5/dia equivale nominalmente a US$ 150 em 30 dias, ou 15% do capital. Além disso, `research/service.py:130` usa `budget_per_call=False`: o gasto acumulado é checado antes do ciclo, mas o ciclo pode ultrapassar o valor restante. Logo, o valor configurado não é um teto rígido de cobrança.

Manter dois indicadores: PnL de trading e PnL econômico, este último descontando LLM, infraestrutura e demais custos atribuíveis. Reservar orçamento por ciclo, limitar tokens/ferramentas pelo saldo e reconciliar estimativas com cobrança real. Definir o orçamento de pesquisa pela contribuição incremental demonstrada, e não só pela tolerância a uma conta diária.

**4. Comissões em BNB e risco nominal incompleto — P1.**

`execution/positions.py:186` calcula o PnL em quote, mas declara que comissões em outros ativos, como BNB, são apenas registradas separadamente. O monitor usa esse PnL. Quando esse modo de comissão for usado, o lucro e a equity ficam superestimados em relação ao custo econômico completo.

Converter cada comissão ao preço contemporâneo do fill e guardar taxa, quantidade, conversão e origem da cotação. Evitar descontar duas vezes comissões já refletidas na quantidade líquida. `risk/monitor.py:41` também calcula PnL aberto sem custo estimado de liquidação e devolve zero quando falta book: distinguir ausência de preço de ausência de perda.

O sizing em `strategy/portfolio.py` usa distância de stop, sem taxas e slippage na perda estimada. Acrescentar esses componentes ao orçamento de risco e testar gaps, spread ampliado e stops executados além do gatilho.

**5. O A/B proposto na documentação ainda não isola o LLM — P1.**

`research/reading.py:35` mantém vetos e multiplicador de exposição mesmo com `llm.weight = 0`. Sem leitura, `ta_only_reduced` reduz exposição pela metade. Portanto, zerar o peso, ou apenas retirar a chave da API, não cria automaticamente um baseline TA puro.

Criar um modo explícito `research_disabled`: nenhuma chamada, veto ou redução causada pelo LLM, preservando os controles determinísticos comuns. Comparar em carteiras virtuais independentes: TA puro, TA com vetos/redução e modelo híbrido atual. Compartilhar snapshots de mercado e hipóteses de execução, mas separar capital, posições e reserva de caixa. Clones na mesma carteira interfeririam entre si pela restrição de um ativo por vez.

O `dry_run` atual registra eventos de entrada; isso sozinho não equivale a uma carteira simulada completa com fills, taxas, capital ocupado e PnL. Implementar esse estado para o A/B. Medir contribuição líquida, drawdown, exposição, giro e oportunidades vetadas; não apenas acerto do sentimento.

**6. O LLM pode promover uma entrada abaixo do limiar técnico — P1.**

`strategy/portfolio.py:56` combina score técnico e sentimento. O limiar em `:118` é aplicado ao resultado combinado. Exemplo aritmético compatível com a fórmula: TA 0,60, peso 0,25 e sentimento/confiança 1 produzem 0,70, acima do mínimo atual 0,67. Havendo setup e passando os demais filtros, uma entrada que falharia no TA puro pode ser aprovada.

O LLM não consegue exceder diretamente o multiplicador máximo de exposição, mas sua autoridade não se limita a vetar/reduzir oportunidades. Isso diverge da leitura forte da promessa arquitetural. Se a intenção for assimetria estrita, exigir aprovação técnica antes da influência do LLM e permitir apenas penalidades, vetos e redução de tamanho. Se o híbrido for intencional, documentar e avaliar essa capacidade separadamente. Acrescentar teste de que o LLM não cria uma entrada inelegível sob a política escolhida.

**7. Simplificar a infraestrutura traz benefício imediato — P1.**

`deploy/stack.yml` inclui SigNoz e seus serviços, além de Grafana, Loki, Alloy, Jaeger, proxy Docker e coletor de métricas. O agente exporta traces para dois destinos. O núcleo é leve; a implantação completa acumula duas soluções de observabilidade.

| Camada | Proposta |
|---|---|
| Núcleo obrigatório | Agente Python + PostgreSQL |
| Operação mínima | Logs JSON com rotação, Telegram, heartbeat externo e backup |
| Painéis opcionais | Grafana consultando PostgreSQL |
| Diagnóstico opcional | Uma única solução de logs/traces, ativada conforme necessidade |
| Pesquisa | SDK do LLM opcional em execução, com orçamento e benefício medido |
| Laboratório | Freqtrade fora do host de produção |

Minha preferência inicial é preservar os painéis SQL existentes no Grafana e retirar a inclusão obrigatória do SigNoz. Loki/Alloy e Jaeger podem ser perfis opcionais. Se a experiência unificada do SigNoz tiver mais valor operacional, escolher essa rota e remover destinos redundantes. Antes de desligar componentes, migrar alertas, métricas essenciais e acessos de investigação.

Medir RAM, CPU, disco/dia e custo por componente durante uma semana comparável; não prometo capacidade de VPS sem esse benchmark. Preservar SQLAlchemy, Alembic, pandas e TA-Lib até haver evidência de gargalo. Reescrever indicadores ou trocar PostgreSQL por SQLite criaria risco de divergência e migração sem ganho demonstrado.

**8. O laboratório ainda pode produzir confiança excessiva — P1.**

A própria documentação mostra `swing_trend` otimizado caindo de +107,27% para −4,95% após introdução do detalhe intrabar. Isso se refere à comparação de métodos documentada, não ao retorno do perfil fixo atual. É evidência de sensibilidade às hipóteses de execução.

Apesar disso, `lab/walk_forward.py:427` ainda deixa `--detail` opcional. Tornar detalhe de 5m padrão nas avaliações de aprovação, com identificação visível de execuções exploratórias sem detalhe. Para situações ambíguas dentro de 5m, adotar ordem conservadora dos eventos e fazer análise de sensibilidade. A documentação do Freqtrade descreve as hipóteses de execução e o uso de timeframe de detalhe. [Freqtrade: backtesting](https://www.freqtrade.io/en/latest/backtesting/).

Adicionar spread, slippage, probabilidade de FOK não preencher, latência após fechamento, mínimos de ordem e limitações do portfólio. O laboratório compartilha sinais, mas não toda a execução, seleção dinâmica, limites globais e influência de pesquisa. Comparar decisões do motor real e do simulador em snapshots idênticos. Fixar imagem do Freqtrade por versão/digest: `lab/freqtrade/docker-compose.yml` usa `stable`.

Guardar os artefatos hoje não versionados de `var/lab/` em armazenamento durável: dados/hashes, commit, configuração, imagem, ordens e curva de equity. Executar também análise de lookahead; ela ajuda a detectar uso de informação futura, sem provar por si só a validade econômica. [Freqtrade: lookahead-analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

**9. Evitar sobreajuste e seleção indireta por parâmetros — P1.**

`doc/lab-results.md` reconhece holdout reutilizado, lista estática de sobreviventes e calibração pela mediana de treinamentos posteriores a parte das janelas. A análise de `alt_atr` envolveu 44 walk-forwards e seleção usando um período já consultado. Os resultados dessa seleção são exploratórios.

O ganho associado à ativação de 10,9% depende de ficar logo abaixo do R:R mínimo quando o stop atinge o teto. Isso embute um filtro de volatilidade em outro parâmetro. Expressar o filtro diretamente, se a hipótese se sustentar, e testar sua estabilidade em uma faixa ampla. Renomear o perfil para refletir a ativação fixa também reduz ambiguidade.

Congelar a estratégia e iniciar avaliação prospectiva com regras de aprovação e encerramento pré-definidas. Não tratar quatro semanas de Demo como comprovação estatística de rentabilidade: elas ajudam a validar operação, mas podem conter poucos trades e um único regime. Construir universo histórico com a informação disponível em cada data, incluindo entradas, saídas e ativos posteriormente deslistados. Sem dados adequados, declarar a limitação.

**10. R:R de ativação não é retorno efetivamente capturado — P1.**

`risk/guard.py:196` usa `(ativação − taxas) / (stop + taxas)`. Com trailing, ativação é início de acompanhamento, não preço de saída. Uma alta de 10,9% seguida imediatamente de recuo de 1,5% corresponde aproximadamente a `(1,109 × 0,985 − 1) = 9,2365%` bruto, antes de custos e execução. A FAQ oficial distingue o gatilho inicial e a distância trailing. [Binance: trailing stop](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/trailing-stop-faq.md).

Separar razão de gatilhos de estimativa conservadora de ganho/perda e medir a distribuição efetiva de saídas. Mudar esse cálculo altera a seleção de `alt_atr`; exige nova validação. Acrescentar risco agregado das posições, concentração por fatores correlacionados e cenários de queda conjunta. A restrição de um ativo por vez não elimina correlação entre altcoins.

O sizing usa capital configurado fixo (`StrategyConfig.profile_capital`), enquanto o monitor acompanha ganhos e perdas. Formalizar se isso é orçamento fixo ou risco percentual da equity. Na segunda opção, ajustar ao capital disponível e perdas, com teto e regra explícita de reinvestimento; não aumentar automaticamente risco para perseguir retorno.

**11. O circuito de depeg merece política própria — P1.**

`risk/monitor.py:59` estima desvio por USDCUSDT e FDUSDUSDT. Esse sinal é relativo a outras stablecoins e não identifica sozinho qual moeda perdeu paridade. `quote_depeg_pct` manda `flatten`, que vende posições para USDT. Isso reduz exposição aos ativos, mas mantém ou aumenta a exposição ao próprio quote potencialmente afetado.

Separar interrupção de novas entradas, redução de exposição cripto e eventual conversão de caixa. Esta última precisa de política explícita, referências independentes e validação operacional. Não fazer conversão automática com base apenas nesses dois pares.

**12. Melhorias de manutenção e desempenho — P2.**

Compartilhar candles fechados e features por símbolo/timeframe/fechamento entre perfis; hoje `_signals()` coleta sequencialmente por perfil. Usar cache em memória com validade e concorrência limitada respeitando rate limits, sem adicionar serviço externo. Priorizar gestão de risco e medir atraso do event loop antes de mover cálculo para workers.

O monitor lê posições fechadas e recalcula agregados a cada snapshot; `Store.closed_positions()` limita a 1.000. Migrar métricas históricas para consultas agregadas por período/perfil e sequência de perdas correta, evitando truncamento silencioso no crescimento da base. Definir retenção para eventos, notícias e telemetria, preservando fills e trilha de auditoria necessários.

Automatizar lint, tipos, testes unitários e integração em CI. Não encontrei diretório `.github`; isso não exclui CI externo. A meta declarada de 100% de cobertura não substitui testes de concorrência, falhas de dados e paridade entre lab e produção. Fixar imagens e registrar manifesto de cada experimento; exigir senhas explícitas em produção, pois o Compose contém defaults de desenvolvimento.

**13. Confiabilidade operacional antes de ampliar capital — P1.**

O checklist do projeto ainda lista heartbeat externo, VPS, backup externo criptografado e restauração testada como pendências. Confirmar o estado real, pois a documentação mistura atualizações de outubro com um status de 03/10. Preservar os runbooks e executar recuperação completa em ambiente isolado, incluindo posições abertas e migrações. Monitorar proteção ausente, atraso de reconciliação, dados obsoletos, orçamento, disco e disponibilidade do banco.

**Sequência de execução proposta.**

1. Corrigir autorização de entrada e desacoplar gestão de posições da pesquisa. Validar halt durante pesquisa, entre ordens e durante flatten.
2. Instrumentar PnL econômico, conversão de comissões e custo por decisão. Medir infraestrutura e remover duplicação de observabilidade.
3. Implementar baseline TA puro e carteiras virtuais isoladas. Avaliar contribuição do LLM com o mesmo risco/exposição e custos atribuídos.
4. Unificar contratos entre simulador e execução, usar detalhe intrabar por padrão e introduzir custos e restrições realistas.
5. Congelar configuração e acompanhar período futuro sem recalibrar pelos resultados observados. Medir expectativa líquida, intervalo de incerteza, drawdown da curva completa, giro, exposição e desempenho por regime.
6. Só ampliar capital após segurança operacional, recuperação e resultado líquido sustentarem a decisão. Comparar com caixa e BTC buy-and-hold, incluindo comparação em exposição/risco semelhante. Se a vantagem não aparecer, reduzir complexidade ou suspender a estratégia é uma decisão válida.

**Métricas de aceite.** Zero entradas autorizadas por estado obsoleto; nenhuma perda de gerenciamento por timeout de pesquisa; custos reconciliados; restore comprovado; universo e configuração reproduzíveis; vantagem incremental do LLM após custo; resultado positivo sob cenários plausíveis de execução. O pior drawdown de uma janela isolada não deve ser apresentado como drawdown máximo de toda a curva. Os limites de aceitação financeira devem ser definidos antes do próximo teste, não escolhidos após observar seus resultados.

**Conclusão.** A stack principal já é apropriada. O maior retorno sobre esforço está em corrigir as fronteiras de risco, consolidar observabilidade, contabilizar o custo total e exigir evidência prospectiva para cada camada adicional. A prioridade não é mais indicadores ou modelos: é verificar que o sistema conserva capital quando precisa parar e que o ganho de trading excede o custo de mantê-lo operando.
