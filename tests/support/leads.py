"""Lead traders of Binance Spot Copy Trading (D-052) for the tests: figures, trades, the
source's answers in its own shape and a scripted source."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trade_agent.leads.source import LeadSourceError, LeadStats, LeadTrade

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
D = Decimal

RISING = tuple(float(day) for day in range(90))
"""A PnL curve that gains 1 USDT a day: every weekly step rises, +30 in the last 30 days."""


def stats(portfolio_id: str = "1", **changes: Any) -> LeadStats:
    """A portfolio that passes every ranking figure of the default selection."""
    values: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "nickname": f"lead-{portfolio_id}",
        "roi_pct": 12.0,
        "mdd_pct": 6.0,
        "sharpe": 1.8,
        "aum": D(5000),
        "copy_aum": D(3000),
        "copier_pnl": D(40),
        "trading_days": 200,
    }
    return LeadStats(**{**values, **changes})


def trade(
    symbol: str = "SOLUSDT",
    *,
    buy: bool = True,
    quote: str = "100",
    price: str = "100",
    hours_ago: float = 1,
    quote_asset: str = "USDT",
    now: datetime = NOW,
) -> LeadTrade:
    return LeadTrade(
        at=now - timedelta(hours=hours_ago),
        symbol=symbol,
        quote_asset=quote_asset,
        buy=buy,
        qty=D(quote) / D(price),
        quote=D(quote),
    )


def active(*extra: LeadTrade, now: datetime = NOW) -> list[LeadTrade]:
    """A human trading rhythm (a trade every three days, the last one a day ago), small
    enough to be no signal by itself, plus ``extra``; the most recent first."""
    routine = [
        trade("BNBUSDT", buy=day % 6 == 0, quote="1", hours_ago=24 + 72 * day, now=now)
        for day in range(6)
    ]
    return sorted([*extra, *routine], key=lambda t: t.at, reverse=True)


# ------------------------------------------------------------------ the source's own shape
def envelope(data: Any, *, code: str = "000000", message: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "messageDetail": None, "data": data, "success": True}


def ranking_row(lead: LeadStats) -> dict[str, Any]:
    return {
        "leadPortfolioId": lead.portfolio_id,
        "nickname": lead.nickname,
        "roi": lead.roi_pct,
        "pnl": 60561.01,
        "aum": float(lead.aum),
        "copyAum": float(lead.copy_aum),
        "mdd": lead.mdd_pct,
        "copierPnl": float(lead.copier_pnl),
        "sharpRatio": lead.sharpe,
        "chartItems": [{"value": 5.9, "dataType": "ROI", "dateTime": 1787356800000}],
        "portfolioType": "PUBLIC",
        "tradingDays": lead.trading_days,
    }


def ranking_payload(leads: Sequence[LeadStats]) -> dict[str, Any]:
    return envelope({"total": len(leads), "list": [ranking_row(lead) for lead in leads]})


def trade_row(item: LeadTrade) -> dict[str, Any]:
    return {
        "time": int(item.at.timestamp() * 1000),
        "symbol": item.symbol,
        "side": "BUY" if item.buy else "SELL",
        "executed": f"{item.qty:f}",
        "baseAsset": item.symbol.removesuffix(item.quote_asset),
        "quoteAsset": item.quote_asset,
        "role": "TAKER",
        "totalAmount": f"{item.quote:f}",
        "fee": "0.1",
        "feeAsset": item.quote_asset,
        "price": "100",
    }


def trades_payload(trades: Sequence[LeadTrade]) -> dict[str, Any]:
    return envelope({"total": len(trades), "list": [trade_row(t) for t in trades]})


def curve_payload(values: Sequence[float], *, start_ms: int = 1787356800000) -> dict[str, Any]:
    day = 86_400_000
    return envelope(
        [
            {"value": value, "dataType": "PNL", "dateTime": start_ms + day * i}
            for i, value in enumerate(values)
        ]
    )


# ------------------------------------------------------------------ a scripted source
class FakeSource:
    """Stands in for :class:`~trade_agent.leads.source.LeadSource`: the ranking, the PnL curve
    and the trades of each portfolio, or the exception to raise instead of any of them."""

    def __init__(
        self,
        ranking: Sequence[LeadStats] = (),
        *,
        curves: Mapping[str, Sequence[float]] | None = None,
        trades: Mapping[str, Sequence[LeadTrade]] | None = None,
        page_size: int = 30,
    ) -> None:
        self.pool = list(ranking)
        self.curves = dict(curves or {})
        self.history = {lead: list(items) for lead, items in (trades or {}).items()}
        self.page_size = page_size
        self.failures: dict[str, Exception] = {}
        """``"ranking"``, ``"curve"`` or ``"trades:<id>"`` → what that request raises."""
        self.calls: list[str] = []

    def _call(self, kind: str, key: str = "") -> None:
        self.calls.append(f"{kind}:{key}" if key else kind)
        failure = self.failures.get(f"{kind}:{key}") or self.failures.get(kind)
        if failure is not None:
            raise failure

    async def ranking(self, page: int) -> list[LeadStats]:
        self._call("ranking", str(page))
        start = (page - 1) * self.page_size
        return self.pool[start : start + self.page_size]

    async def pnl_curve(self, portfolio_id: str) -> tuple[float, ...]:
        self._call("curve", portfolio_id)
        return tuple(self.curves.get(portfolio_id, RISING))

    async def trades(self, portfolio_id: str) -> list[LeadTrade]:
        self._call("trades", portfolio_id)
        return list(self.history.get(portfolio_id, ()))

    def fail(self, request: str, error: Exception | None = None) -> None:
        self.failures[request] = error or LeadSourceError("HTTP 500")
