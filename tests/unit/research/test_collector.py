from typing import Any

import httpx
import pytest
import respx

from tests.support.claude import NOW
from trade_agent.research import sources
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import SourcesConfig
from trade_agent.research.models import DerivativesSnapshot
from trade_agent.research.tagging import AssetTagger

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Solana outage hits SOL</title><link>https://feed.example/1</link></item>
<item><title>Solana outage hits SOL</title><link>https://feed.example/1</link></item>
</channel></rss>"""

CMS = {
    "code": "000000",
    "data": {
        "catalogs": [
            {
                "articles": [
                    {"code": "x1", "title": "Binance Will Delist ZEPH", "releaseDate": 1.79e12}
                ]
            }
        ]
    },
}


def _config(**overrides: Any) -> SourcesConfig:
    data: dict[str, Any] = {
        "rss_feeds": {"feed": "https://feed.example/rss", "down": "https://down.example/rss"},
        "binance_catalogs": {161: "delistagens"},
        "derivatives_max_symbols": 2,
    }
    data.update(overrides)
    return SourcesConfig(**data)


def _mock_all(router: respx.MockRouter) -> None:
    router.get("https://feed.example/rss").respond(200, content=FEED)
    router.get("https://down.example/rss").respond(503)
    router.get(sources.BINANCE_CMS).respond(200, json=CMS)
    router.get(sources.FEAR_GREED).respond(
        200, json={"data": [{"value": "30", "value_classification": "Fear"}]}
    )
    router.get(f"{sources.FAPI}/fapi/v1/premiumIndex").respond(
        200,
        json=[
            {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"},
            {"symbol": "ETHUSDT", "lastFundingRate": "0.0002"},
        ],
    )
    router.get(
        f"{sources.FAPI}/futures/data/openInterestHist", params={"symbol": "BTCUSDT"}
    ).respond(200, json=[{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "120"}])
    router.get(
        f"{sources.FAPI}/futures/data/openInterestHist", params={"symbol": "ETHUSDT"}
    ).respond(200, json=[])


async def test_collect_all_sources_isolating_failures() -> None:
    tagger = AssetTagger(["SOL", "ZEPH"], {"SOL": ["solana"]})
    with respx.mock(assert_all_called=False) as router:
        _mock_all(router)
        async with httpx.AsyncClient() as http:
            collector = NewsCollector(http, _config(), tagger, clock=lambda: NOW)
            result = await collector.collect(
                derivative_symbols=["BTCUSDT", "ETHUSDT", "NOPEUSDT"], btc_change_24h=-0.02
            )
    assert list(result.errors) == ["rss:down"]
    assert "503" in result.errors["rss:down"]
    assert sorted((i.source, i.assets) for i in result.items) == [
        ("binance", ("ZEPH",)),
        ("feed", ("SOL",)),  # duplicate removed
    ]
    metrics = result.metrics
    assert metrics.fear_greed is not None and metrics.fear_greed.value == 30
    assert metrics.btc_change_24h == -0.02
    assert metrics.derivatives.funding_rate == {"BTCUSDT": 0.0001, "ETHUSDT": 0.0002}
    assert metrics.derivatives.open_interest_change_24h == {"BTCUSDT": pytest.approx(0.2)}


async def test_collect_with_sources_disabled() -> None:
    config = _config(rss_feeds={}, binance_catalogs={}, fear_greed=False, derivatives=True)
    with respx.mock() as router:
        async with httpx.AsyncClient() as http:
            result = await NewsCollector(http, config, AssetTagger([], {})).collect()
    assert result.items == () and result.errors == {}
    assert result.metrics.fear_greed is None
    assert result.metrics.derivatives == DerivativesSnapshot()
    assert not router.calls


async def test_fetch_derivatives_skips_symbols_without_perpetual() -> None:
    with respx.mock() as router:
        router.get(f"{sources.FAPI}/fapi/v1/premiumIndex").respond(200, json=[])
        async with httpx.AsyncClient() as http:
            snapshot = await sources.fetch_derivatives(http, ["XUSDT"])
            assert await sources.fetch_derivatives(http, []) == DerivativesSnapshot()
    assert snapshot == DerivativesSnapshot()
