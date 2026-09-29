"""Coleta de todas as fontes com isolamento de falhas: uma fonte fora do ar não impede
as outras (o erro fica registrado no resultado)."""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from trade_agent.research.config import SourcesConfig
from trade_agent.research.models import DerivativesSnapshot, FearGreed, MarketMetrics, NewsItem
from trade_agent.research.sources import (
    fetch_announcements,
    fetch_derivatives,
    fetch_fear_greed,
    fetch_feed,
)
from trade_agent.research.tagging import AssetTagger

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Collection:
    items: tuple[NewsItem, ...]
    metrics: MarketMetrics
    errors: Mapping[str, str] = field(default_factory=dict)


class NewsCollector:
    def __init__(
        self,
        http: httpx.AsyncClient,
        config: SourcesConfig,
        tagger: AssetTagger,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._config = config
        self._tagger = tagger
        self._clock = clock

    async def collect(
        self, *, derivative_symbols: Sequence[str] = (), btc_change_24h: float | None = None
    ) -> Collection:
        now = self._clock()
        config = self._config
        jobs: dict[str, Awaitable[Any]] = {}
        if config.binance_catalogs:
            jobs["binance"] = fetch_announcements(self._http, config.binance_catalogs)
        for name, url in config.rss_feeds.items():
            jobs[f"rss:{name}"] = fetch_feed(self._http, name, url, now=now)
        if config.fear_greed:
            jobs["fear_greed"] = fetch_fear_greed(self._http)
        symbols = list(derivative_symbols)[: config.derivatives_max_symbols]
        if config.derivatives and symbols:
            jobs["derivatives"] = fetch_derivatives(self._http, symbols)

        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        errors: dict[str, str] = {}
        items: dict[str, NewsItem] = {}
        fear_greed: FearGreed | None = None
        derivatives = DerivativesSnapshot()
        for name, result in zip(jobs, results, strict=True):
            if isinstance(result, BaseException):
                errors[name] = f"{type(result).__name__}: {result}"
                log.debug("research.source_failed", source=name, error=errors[name])
            elif isinstance(result, FearGreed):
                fear_greed = result
            elif isinstance(result, DerivativesSnapshot):
                derivatives = result
            else:
                log.debug("research.source", source=name, items=len(result))
                for item in result:
                    tagged = dataclasses.replace(
                        item, assets=self._tagger.tag(f"{item.title} {item.summary}")
                    )
                    items.setdefault(tagged.dedupe_key, tagged)
        metrics = MarketMetrics(fear_greed, derivatives, btc_change_24h)
        log.debug(
            "research.collected",
            items=len(items),
            sources=len(jobs),
            failed=sorted(errors),
            fear_greed=fear_greed.value if fear_greed else None,
            funding_symbols=len(derivatives.funding_rate),
        )
        return Collection(tuple(items.values()), metrics, errors)
