"""The public source of the lead traders (D-052): its answers parsed and every surprise
turned into ``LeadSourceError``."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from tests.support.leads import (
    NOW,
    curve_payload,
    envelope,
    ranking_payload,
    ranking_row,
    stats,
    trade,
    trade_row,
    trades_payload,
)
from trade_agent.leads.source import (
    PNL_CURVE,
    RANKING,
    TRADES,
    LeadSource,
    LeadSourceError,
    LeadSourceRefusedError,
    parse_pnl_curve,
    parse_ranking,
    parse_trades,
)

D = Decimal


def test_a_ranking_row_becomes_the_portfolios_figures() -> None:
    lead = stats("4457572105734768129", sharpe=-0.2, copier_pnl=D("-59357.8"))
    (parsed,) = parse_ranking(ranking_payload([lead]))
    assert parsed == lead
    assert parsed.balance == D(2000)  # its AUM minus the copiers' money


def test_a_young_portfolio_has_no_sharpe_and_optional_figures_default() -> None:
    row = ranking_row(stats())
    row["sharpRatio"] = None
    for optional in ("copyAum", "copierPnl", "tradingDays", "nickname"):
        del row[optional]
    (parsed,) = parse_ranking(envelope({"list": [row]}))
    assert parsed.sharpe is None
    assert (parsed.copy_aum, parsed.copier_pnl, parsed.trading_days, parsed.nickname) == (
        D(0),
        D(0),
        0,
        "",
    )
    assert parsed.balance == parsed.aum


def test_trades_come_back_the_most_recent_first() -> None:
    old = trade("ETHUSDT", buy=False, quote="250", hours_ago=30)
    new = trade("SOLUSDT", quote="100", price="50", hours_ago=1)
    assert parse_trades(trades_payload([old, new])) == [new, old]
    assert (new.qty, new.quote, new.buy, old.buy) == (D(2), D(100), True, False)


def test_the_pnl_curve_is_ordered_by_day() -> None:
    payload = curve_payload([10.0, 12.5, 11.0])
    payload["data"].reverse()
    assert parse_pnl_curve(payload) == (10.0, 12.5, 11.0)
    assert parse_pnl_curve(envelope(None)) == ()  # a portfolio without a curve yet


@pytest.mark.parametrize(
    ("parse", "payload", "message"),
    [
        (parse_ranking, "<html>", "fora do formato"),
        (parse_ranking, envelope(None, code="11081031", message="closed"), "11081031: closed"),
        (parse_ranking, envelope(None), "sem data.list"),
        (parse_ranking, envelope({"total": 1}), "sem data.list"),
        (parse_ranking, envelope({"list": {"a": 1}}), "não é uma lista"),
        (parse_ranking, envelope({"list": [{"nickname": "no id"}]}), "ranking fora do formato"),
        (parse_ranking, envelope({"list": [{**ranking_row(stats()), "aum": "x"}]}), "ranking"),
        (parse_trades, envelope({"list": [{"symbol": "SOLUSDT"}]}), "histórico fora do formato"),
        (
            parse_trades,
            envelope({"list": [{**trade_row(trade()), "side": "HOLD"}]}),
            "lado desconhecido",
        ),
        (parse_pnl_curve, envelope([{"value": "x", "dateTime": 1}]), "curva de PnL"),
        (parse_pnl_curve, envelope([{"dateTime": 1}]), "curva de PnL"),
        (parse_pnl_curve, envelope(7), "curva de PnL"),
    ],
)
def test_an_unexpected_answer_is_a_source_error(parse: Any, payload: Any, message: str) -> None:
    with pytest.raises(LeadSourceError, match=message):
        parse(payload)


@respx.mock
async def test_the_three_readings_ask_only_the_public_endpoints() -> None:
    lead = stats("77")
    ranking = respx.post(RANKING).respond(json=ranking_payload([lead]))
    history = respx.post(TRADES).respond(json=trades_payload([trade(now=NOW)]))
    curve = respx.get(PNL_CURVE).respond(json=curve_payload([1.0, 2.0]))
    async with httpx.AsyncClient() as http:
        source = LeadSource(http)
        assert await source.ranking(2) == [lead]
        assert await source.trades("77") == [trade(now=NOW)]
        assert await source.pnl_curve("77") == (1.0, 2.0)
    asked = json.loads(ranking.calls.last.request.content)
    assert asked == {
        "pageNumber": 2,
        "pageSize": 30,
        "timeRange": "90D",
        "dataType": "SHARP_RATIO",
        "order": "DESC",
        "portfolioType": "PUBLIC",
    }
    assert json.loads(history.calls.last.request.content) == {
        "portfolioId": "77",
        "pageNumber": 1,
        "pageSize": 100,
    }
    assert dict(curve.calls.last.request.url.params) == {
        "dataType": "PNL",
        "portfolioId": "77",
        "timeRange": "90D",
    }
    request = ranking.calls.last.request
    assert request.headers["user-agent"].startswith("trade-agent/")
    assert "cookie" not in request.headers and "x-mbx-apikey" not in request.headers


@respx.mock
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, LeadSourceRefusedError),
        (418, LeadSourceRefusedError),
        (429, LeadSourceRefusedError),
        (451, LeadSourceRefusedError),
        (500, LeadSourceError),
        (404, LeadSourceError),
    ],
)
async def test_an_http_error_is_a_source_error_and_a_refusal_its_own(
    status: int, error: type[LeadSourceError]
) -> None:
    respx.post(RANKING).respond(status)
    async with httpx.AsyncClient() as http:
        with pytest.raises(LeadSourceError, match=f"HTTP {status}") as raised:
            await LeadSource(http).ranking(1)
    assert type(raised.value) is error


@respx.mock
async def test_a_network_failure_and_a_page_that_is_not_json_are_source_errors() -> None:
    respx.post(TRADES).mock(side_effect=httpx.ConnectTimeout("too slow"))
    respx.get(PNL_CURVE).respond(text="<html>blocked</html>")
    async with httpx.AsyncClient() as http:
        source = LeadSource(http)
        with pytest.raises(LeadSourceError, match="ConnectTimeout: too slow"):
            await source.trades("1")
        with pytest.raises(LeadSourceError, match="não é JSON"):
            await source.pnl_curve("1")
