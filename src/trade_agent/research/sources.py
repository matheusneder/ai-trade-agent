"""Coleta de notícias e métricas públicas (sem chave).

* Anúncios do site da Binance (listagens, delistagens, notícias);
* feeds RSS/Atom de mídia cripto;
* índice Fear & Greed (alternative.me);
* *funding* e *open interest* da Binance Futures USDⓈ-M.

As funções ``parse_*`` são puras; as ``fetch_*`` só fazem o HTTP.
"""

import calendar
import html
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import feedparser  # type: ignore[import-untyped]
import httpx

from trade_agent.research.models import DerivativesSnapshot, FearGreed, NewsItem

BINANCE_CMS = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
BINANCE_ARTICLE = "https://www.binance.com/en/support/announcement/{code}"
FEAR_GREED = "https://api.alternative.me/fng/"
FAPI = "https://fapi.binance.com"
USER_AGENT = {"User-Agent": "trade-agent/0.1 (+research)"}

_TAG = re.compile(r"<[^>]+>")


def clean_html(value: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", value)).split())


# ============================================================================ parse (puras)
def parse_fear_greed(data: Mapping[str, Any]) -> FearGreed:
    rows = data["data"]
    return FearGreed(
        value=int(rows[0]["value"]),
        classification=str(rows[0]["value_classification"]),
        previous=int(rows[1]["value"]) if len(rows) > 1 else None,
    )


def parse_announcements(data: Mapping[str, Any], label: str) -> list[NewsItem]:
    if data.get("code") != "000000":
        raise ValueError(f"resposta inesperada do CMS da Binance: {data.get('code')}")
    items: list[NewsItem] = []
    for catalog in data["data"]["catalogs"]:
        for article in catalog.get("articles", []):
            items.append(
                NewsItem(
                    source="binance",
                    title=str(article["title"]),
                    url=BINANCE_ARTICLE.format(code=article["code"]),
                    published_at=datetime.fromtimestamp(article["releaseDate"] / 1000, UTC),
                    summary=label,
                )
            )
    return items


def parse_feed(content: bytes, source: str, *, now: datetime) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        title = clean_html(str(entry.get("title", "")))
        if not title:
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(stamp), UTC) if stamp else now
        items.append(
            NewsItem(
                source=source,
                title=title,
                url=entry.get("link") or None,
                published_at=published,
                summary=clean_html(str(entry.get("summary", ""))),
            )
        )
    return items


def parse_funding(rows: Sequence[Mapping[str, Any]], symbols: Sequence[str]) -> dict[str, float]:
    wanted = set(symbols)
    return {
        str(r["symbol"]): float(r["lastFundingRate"])
        for r in rows
        if r["symbol"] in wanted and r.get("lastFundingRate") not in (None, "")
    }


def open_interest_change(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Variação do *open interest* (USDT) entre o primeiro e o último ponto."""
    if len(rows) < 2:
        return None
    first = float(rows[0]["sumOpenInterestValue"])
    last = float(rows[-1]["sumOpenInterestValue"])
    return None if first <= 0 else last / first - 1


# ============================================================================ fetch (HTTP)
async def _get_json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    response = await client.get(url, params=params or None, headers=USER_AGENT)
    response.raise_for_status()
    return response.json()


async def fetch_fear_greed(client: httpx.AsyncClient) -> FearGreed:
    return parse_fear_greed(await _get_json(client, FEAR_GREED, limit=2))


async def fetch_announcements(
    client: httpx.AsyncClient, catalogs: Mapping[int, str], *, page_size: int = 20
) -> list[NewsItem]:
    items: list[NewsItem] = []
    for catalog, label in catalogs.items():
        data = await _get_json(
            client, BINANCE_CMS, type=1, pageNo=1, pageSize=page_size, catalogId=catalog
        )
        items.extend(parse_announcements(data, label))
    return items


async def fetch_feed(
    client: httpx.AsyncClient, source: str, url: str, *, now: datetime
) -> list[NewsItem]:
    response = await client.get(url, headers=USER_AGENT, follow_redirects=True)
    response.raise_for_status()
    return parse_feed(response.content, source, now=now)


async def fetch_derivatives(
    client: httpx.AsyncClient, symbols: Sequence[str]
) -> DerivativesSnapshot:
    if not symbols:
        return DerivativesSnapshot()
    funding = parse_funding(await _get_json(client, f"{FAPI}/fapi/v1/premiumIndex"), symbols)
    changes: dict[str, float] = {}
    for symbol in symbols:
        if symbol not in funding:  # sem contrato perpétuo: não há open interest
            continue
        rows = await _get_json(
            client, f"{FAPI}/futures/data/openInterestHist", symbol=symbol, period="1h", limit=25
        )
        change = open_interest_change(rows)
        if change is not None:
            changes[symbol] = change
    return DerivativesSnapshot(funding_rate=funding, open_interest_change_24h=changes)
