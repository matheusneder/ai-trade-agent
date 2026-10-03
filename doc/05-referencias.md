# 05 — Referências

Documentação externa de que o projeto depende.

## Binance — API Spot

- Documentação da API Spot (repositório oficial): https://github.com/binance/binance-spot-api-docs
- Endpoints de negociação (OCO, OTO, OTOCO, OPO, OPOCO, cancelReplace, amend): https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints
- REST API (markdown completo): https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- FAQ OPO (*One Pays the Other*): https://github.com/binance/binance-spot-api-docs/blob/master/faqs/opo.md
- FAQ de trailing stop: https://developers.binance.com/docs/binance-spot-api-docs/faqs/trailing-stop-faq
- Filtros (`MAX_NUM_ALGO_ORDERS`, `TRAILING_DELTA`, `NOTIONAL` etc.): https://github.com/binance/binance-spot-api-docs/blob/master/filters.md
- *Price Range Execution Rule*: https://github.com/binance/binance-spot-api-docs/blob/master/faqs/price_range_execution_rules.md
- Changelog da API (Demo Mode, rate limits, fim do `listenKey`, `serverShutdown`): https://developers.binance.com/docs/binance-spot-api-docs/CHANGELOG
- Demo Mode (*paper trading* pela API): https://github.com/binance/binance-spot-api-docs/blob/master/demo-mode/general-info.md
- Spot Testnet: https://testnet.binance.vision
- Cronograma de delistagem (`/sapi/v1/spot/delist-schedule`): https://developers.binance.com/docs/wallet/others/delist-schedule
- Subcontas (elegibilidade): https://www.binance.com/en/support/faq/binance-sub-account-functions-and-frequently-asked-questions-360020632811
- Erro 451 (local restrito), discussão da comunidade: https://dev.binance.vision/t/error-restricted-location-according-b-eligibility/13838

## LLM (Anthropic / Claude)

- Preços (modelos, *prompt caching*, busca web a US$ 10 por 1.000): https://platform.claude.com/docs/en/about-claude/pricing
- Documentação da API (*structured outputs*, ferramentas de servidor, *prompt caching*): https://platform.claude.com/docs

## Telegram

- Bot API (`sendMessage`, `getUpdates`): https://core.telegram.org/bots/api

## Dados de mercado e notícias

- Fear & Greed Index (alternative.me): https://api.alternative.me/fng/
- Os feeds RSS e os catálogos de anúncios da Binance estão em `config/research.yaml`.

## Laboratório

- Freqtrade — backtesting: https://www.freqtrade.io/en/stable/backtesting/
- Freqtrade — *hyperopt*: https://www.freqtrade.io/en/stable/hyperopt/
- Freqtrade — download de dados (`--prepend`): https://www.freqtrade.io/en/stable/data-download/

## Observabilidade

- Grafana (alerting com *contact point* Telegram): https://grafana.com/docs/grafana/latest/alerting/
- Grafana Loki: https://grafana.com/docs/loki/latest/
- Grafana Alloy: https://grafana.com/docs/alloy/latest/
- Jaeger: https://www.jaegertracing.io/docs/
- OpenTelemetry para Python: https://opentelemetry.io/docs/languages/python/
- SigNoz: https://signoz.io/docs/
- docker-socket-proxy (Tecnativa): https://github.com/Tecnativa/docker-socket-proxy
- Healthchecks.io: https://healthchecks.io
