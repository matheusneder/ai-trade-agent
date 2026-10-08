# 05 — References

External documentation the project depends on.

## Binance — Spot API

- Spot API documentation (official repository): https://github.com/binance/binance-spot-api-docs
- Trading endpoints (OCO, OTO, OTOCO, OPO, OPOCO, cancelReplace, amend): https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints
- REST API (full markdown): https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- OPO FAQ (*One Pays the Other*): https://github.com/binance/binance-spot-api-docs/blob/master/faqs/opo.md
- Trailing stop FAQ: https://developers.binance.com/docs/binance-spot-api-docs/faqs/trailing-stop-faq
- Filters (`MAX_NUM_ALGO_ORDERS`, `TRAILING_DELTA`, `NOTIONAL` etc.): https://github.com/binance/binance-spot-api-docs/blob/master/filters.md
- *Price Range Execution Rule*: https://github.com/binance/binance-spot-api-docs/blob/master/faqs/price_range_execution_rules.md
- API changelog (Demo Mode, rate limits, end of `listenKey`, `serverShutdown`): https://developers.binance.com/docs/binance-spot-api-docs/CHANGELOG
- Demo Mode (*paper trading* through the API): https://github.com/binance/binance-spot-api-docs/blob/master/demo-mode/general-info.md
- Spot Testnet: https://testnet.binance.vision
- Delisting schedule (`/sapi/v1/spot/delist-schedule`): https://developers.binance.com/docs/wallet/others/delist-schedule
- Sub-accounts (eligibility): https://www.binance.com/en/support/faq/binance-sub-account-functions-and-frequently-asked-questions-360020632811
- Error 451 (restricted location), community discussion: https://dev.binance.vision/t/error-restricted-location-according-b-eligibility/13838

## LLM (Anthropic / Claude)

- Pricing (models, *prompt caching*, web search at US$ 10 per 1,000): https://platform.claude.com/docs/en/about-claude/pricing
- API documentation (*structured outputs*, server tools, *prompt caching*): https://platform.claude.com/docs

## Telegram

- Bot API (`sendMessage`, `getUpdates`): https://core.telegram.org/bots/api

## Market data and news

- Fear & Greed Index (alternative.me): https://api.alternative.me/fng/
- The RSS feeds and the Binance announcement catalogs are in `config/research.yaml`.

## Lab

- Freqtrade — backtesting: https://www.freqtrade.io/en/stable/backtesting/
- Freqtrade — *hyperopt*: https://www.freqtrade.io/en/stable/hyperopt/
- Freqtrade — data download (`--prepend`): https://www.freqtrade.io/en/stable/data-download/

## Observability

- Grafana (alerting with a Telegram *contact point*): https://grafana.com/docs/grafana/latest/alerting/
- Grafana Loki: https://grafana.com/docs/loki/latest/
- Grafana Alloy: https://grafana.com/docs/alloy/latest/
- Jaeger: https://www.jaegertracing.io/docs/
- OpenTelemetry for Python: https://opentelemetry.io/docs/languages/python/
- SigNoz: https://signoz.io/docs/
- docker-socket-proxy (Tecnativa): https://github.com/Tecnativa/docker-socket-proxy
- Healthchecks.io: https://healthchecks.io
