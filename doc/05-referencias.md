# 05 — Referências

Consultadas em 25/09/2026.

## Binance — API Spot e ferramentas

- Documentação da API Spot (repositório oficial): https://github.com/binance/binance-spot-api-docs
- Endpoints de negociação (OCO, OTO, OTOCO, OPO, OPOCO, cancelReplace, amend): https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints
- REST API (markdown completo): https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- FAQ OPO (*One Pays the Other*): https://github.com/binance/binance-spot-api-docs/blob/master/faqs/opo.md
- FAQ de trailing stop: https://developers.binance.com/docs/binance-spot-api-docs/faqs/trailing-stop-faq
- Filtros (`MAX_NUM_ALGO_ORDERS`, `TRAILING_DELTA`, `NOTIONAL` etc.): https://github.com/binance/binance-spot-api-docs/blob/master/filters.md
- *Price Range Execution Rule*: https://github.com/binance/binance-spot-api-docs/blob/master/faqs/price_range_execution_rules.md
- Changelog da API (Demo Mode, rate limits, fim do `listenKey`, `serverShutdown`): https://developers.binance.com/docs/binance-spot-api-docs/CHANGELOG
- Demo Mode (paper trading pela API): https://github.com/binance/binance-spot-api-docs/blob/master/demo-mode/general-info.md
- Spot Testnet: https://testnet.binance.vision
- Cronograma de delistagem (`/sapi/v1/spot/delist-schedule`): https://developers.binance.com/docs/wallet/others/delist-schedule
- Subcontas (elegibilidade): https://www.binance.com/en/support/faq/binance-sub-account-functions-and-frequently-asked-questions-360020632811
- SDK Python oficial `binance-sdk-spot`: https://pypi.org/project/binance-sdk-spot/ · https://github.com/binance/binance-connector-python
- Binance Skills Hub: https://github.com/binance/binance-skills-hub · https://developers.binance.info/en/docs/sdks-tools/integrations/skills-hub
- Erro 451 (local restrito) — discussão da comunidade: https://dev.binance.vision/t/error-restricted-location-according-b-eligibility/13838

## Frameworks e projetos open source

- Freqtrade — stoploss (inclusive *on exchange*): https://www.freqtrade.io/en/stable/stoploss/
- Freqtrade — pairlists e protections (RemotePairList, MaxDrawdown…): https://www.freqtrade.io/en/stable/plugins/
- Freqtrade — issue “take profit on exchange” (#7499): https://github.com/freqtrade/freqtrade/issues/7499
- Freqtrade — releases: https://github.com/freqtrade/freqtrade/releases
- Hummingbot API: https://hummingbot.org/hummingbot-api/
- Hummingbot Position Executor (*triple barrier*): https://hummingbot.org/v2-strategies/executors/positionexecutor/
- Condor (harness de agentes do Hummingbot): https://hummingbot.org/blog/introducing-condor-the-open-source-harness-for-trading-agents/ · https://github.com/hummingbot/condor
- OctoBot: https://github.com/Drakkar-Software/OctoBot
- TradingAgents (LangGraph, multiagente): https://github.com/TauricResearch/TradingAgents
- NautilusTrader: https://nautilustrader.io
- Jesse: https://jesse.trade

## LLM (Anthropic / Claude)

- Preços (modelos, *prompt caching*, *batch*, busca web US$ 10/1.000): https://platform.claude.com/docs/en/about-claude/pricing
- Documentação da API (structured outputs, ferramentas de servidor, *prompt caching*): https://platform.claude.com/docs

## Dados de mercado e sentimento

- Fear & Greed Index (alternative.me): https://api.alternative.me/fng/
- Comparativo de APIs cripto gratuitas em 2026 (CoinMarketCap Academy): https://coinmarketcap.com/academy/article/best-free-crypto-api-in-2026-free-tier-comparison
- CryptoPanic — planos de API: https://cryptopanic.com/developers/api/plans
- CoinGecko API: https://www.coingecko.com/en/api

## Operação e observabilidade

- Grafana (alerting com *contact point* Telegram): https://grafana.com/docs/grafana/latest/alerting/
- Healthchecks.io: https://healthchecks.io
