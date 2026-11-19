"""Binance Spot Copy Trading: the public ranking of the lead portfolios and their trades.

The official Copy Trading API (``/sapi/v1/copyTrading/...``) only tells a lead trader their
own status. The ranking, the performance figures and the trades of the lead portfolios exist
only on the endpoints Binance's own website calls (``www.binance.com/bapi/...``): public and
keyless, but undocumented and with no guarantee of staying as they are (D-052). Every answer
is therefore checked here, and any surprise is a :class:`LeadSourceError`, which the tracker
treats as the source being unavailable.

Three readings: the 90-day ranking (figures per portfolio), a portfolio's cumulative PnL per
day of that window, and its 100 most recent trades. The ranking's own ROI curve is not read:
a deposit dilutes the ROI (a portfolio seen on 2026-11-19 went from +84% to +23% in three
days with its PnL unchanged), so the weekly steps and the recent gain come from the PnL.

The ``parse_*`` functions are pure; :class:`LeadSource` only does the HTTP.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from opentelemetry.trace import SpanKind

from trade_agent import tracing

BASE = "https://www.binance.com/bapi/futures/v1"
RANKING = f"{BASE}/friendly/future/spot-copy-trade/common/home-page-list"
TRADES = f"{BASE}/friendly/future/spot-copy-trade/lead-portfolio/get-trade-history-by-page"
PNL_CURVE = f"{BASE}/public/future/spot-copy-trade/lead-portfolio/performance-chart-data"
HEADERS = {"User-Agent": "trade-agent/0.1 (+leads)"}
OK = "000000"
PAGE_SIZE = 30
"""Portfolios per ranking page."""
TRADES_PAGE = 100
"""The most recent trades of a portfolio in one request: the endpoint returns no more."""
RANKING_WINDOW = "90D"
REFUSED = frozenset({403, 418, 429, 451})
"""HTTP statuses that mean "stop asking": forbidden, banned, rate limited, restricted region."""


class LeadSourceError(Exception):
    """The source failed or answered something unexpected."""


class LeadSourceRefusedError(LeadSourceError):
    """The source refuses the requests: the tracker stops asking for a while."""


@dataclass(frozen=True, slots=True)
class LeadStats:
    """A lead portfolio's row of the ranking, with the figures of the ranking's window."""

    portfolio_id: str
    nickname: str
    roi_pct: float
    mdd_pct: float
    sharpe: float | None
    """Binance gives none to a portfolio younger than about a month."""
    aum: Decimal
    copy_aum: Decimal
    copier_pnl: Decimal
    trading_days: int

    @property
    def balance(self) -> Decimal:
        """The lead trader's own money in the portfolio: its AUM minus the copiers'."""
        return self.aum - self.copy_aum


@dataclass(frozen=True, slots=True)
class PnlPoint:
    """A day of a portfolio's cumulative PnL curve (quote asset)."""

    at: datetime
    pnl: float


@dataclass(frozen=True, slots=True)
class LeadTrade:
    at: datetime
    symbol: str
    quote_asset: str
    buy: bool
    qty: Decimal
    quote: Decimal
    """What the trade moved, in the quote asset."""


# ============================================================================ parse (pure)
def _rows(data: Any) -> list[Any]:
    """The ``data.list`` of an answer."""
    try:
        rows = _data(data)["list"]
    except (KeyError, TypeError) as exc:
        raise LeadSourceError("resposta sem data.list") from exc
    if not isinstance(rows, list):
        raise LeadSourceError("data.list não é uma lista")
    return rows


def _at(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, UTC)


def _data(data: Any) -> Any:
    """The ``data`` of an answer, after its envelope."""
    if not isinstance(data, Mapping):
        raise LeadSourceError("resposta fora do formato esperado")
    if data.get("code") != OK:
        raise LeadSourceError(f"resposta {data.get('code')}: {data.get('message')}")
    return data.get("data")


def _stats(row: Mapping[str, Any]) -> LeadStats:
    sharpe = row.get("sharpRatio")
    return LeadStats(
        portfolio_id=str(row["leadPortfolioId"]),
        nickname=str(row.get("nickname") or ""),
        roi_pct=float(row["roi"]),
        mdd_pct=float(row["mdd"]),
        sharpe=None if sharpe is None else float(sharpe),
        aum=Decimal(str(row["aum"])),
        copy_aum=Decimal(str(row.get("copyAum") or 0)),
        copier_pnl=Decimal(str(row.get("copierPnl") or 0)),
        trading_days=int(row.get("tradingDays") or 0),
    )


def _trade(row: Mapping[str, Any]) -> LeadTrade:
    side = str(row["side"]).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"lado desconhecido: {side}")
    return LeadTrade(
        at=_at(row["time"]),
        symbol=str(row["symbol"]),
        quote_asset=str(row["quoteAsset"]),
        buy=side == "BUY",
        qty=Decimal(str(row["executed"])),
        quote=Decimal(str(row["totalAmount"])),
    )


def parse_ranking(data: Any) -> list[LeadStats]:
    try:
        return [_stats(row) for row in _rows(data)]
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise LeadSourceError(f"ranking fora do formato esperado: {exc!r}") from exc


def parse_trades(data: Any) -> list[LeadTrade]:
    """A portfolio's trades, the most recent first."""
    try:
        trades = [_trade(row) for row in _rows(data)]
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise LeadSourceError(f"histórico fora do formato esperado: {exc!r}") from exc
    return sorted(trades, key=lambda t: t.at, reverse=True)


def parse_pnl_curve(data: Any) -> tuple[PnlPoint, ...]:
    """A portfolio's cumulative PnL (quote asset) on each day of the ranking's window, the
    oldest first, with its date: a day may be missing, so windows go by the calendar."""
    try:
        points = sorted((int(p["dateTime"]), float(p["value"])) for p in _data(data) or ())
    except (KeyError, TypeError, ValueError) as exc:
        raise LeadSourceError(f"curva de PnL fora do formato esperado: {exc!r}") from exc
    return tuple(PnlPoint(_at(ms), value) for ms, value in points)


# ============================================================================ fetch (HTTP)
class LeadSource:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def ranking(self, page: int) -> list[LeadStats]:
        """A page of the 90-day ranking of the public portfolios, best Sharpe ratio first."""
        body = {
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "timeRange": RANKING_WINDOW,
            "dataType": "SHARP_RATIO",
            "order": "DESC",
            "portfolioType": "PUBLIC",
        }
        return parse_ranking(await self._request("POST", RANKING, json=body))

    async def trades(self, portfolio_id: str) -> list[LeadTrade]:
        body = {"portfolioId": portfolio_id, "pageNumber": 1, "pageSize": TRADES_PAGE}
        return parse_trades(await self._request("POST", TRADES, json=body))

    async def pnl_curve(self, portfolio_id: str) -> tuple[PnlPoint, ...]:
        params = {"dataType": "PNL", "portfolioId": portfolio_id, "timeRange": RANKING_WINDOW}
        return parse_pnl_curve(await self._request("GET", PNL_CURVE, params=params))

    async def _request(self, method: str, url: str, **content: Any) -> Any:
        """One request in a client span: the path only, never the portfolio."""
        attributes = tracing.http_attributes(method, url)
        with tracing.span(
            "leads", f"{method} {attributes['server.address']}", kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:  # fmt: skip
            try:
                response = await self._http.request(method, url, headers=HEADERS, **content)
            except httpx.HTTPError as exc:
                raise LeadSourceError(f"{type(exc).__name__}: {exc}") from exc
            status = response.status_code
            span.set_attribute("http.response.status_code", status)
            if status in REFUSED:
                raise LeadSourceRefusedError(f"HTTP {status}")
            if status >= 400:
                raise LeadSourceError(f"HTTP {status}")
            try:
                return response.json()
            except ValueError as exc:
                raise LeadSourceError("resposta não é JSON") from exc
