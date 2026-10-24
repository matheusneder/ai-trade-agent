from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from tests.support.tape import BASE_MS, FakeTape, at
from trade_agent.demo_audit import (
    MARKET,
    BinanceTape,
    DemoExit,
    Exit,
    ExitLeg,
    LegKind,
    PositionAudit,
    Segment,
    demo_exit,
    render,
    replay,
    segments_from,
    summary,
)
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.persistence.models import ExchangeOrderRecord, FillRecord
from trade_agent.persistence.store import Intent, IntentKind, IntentStatus

D = Decimal
TP = LegKind.TAKE_PROFIT
SL = LegKind.STOP_LOSS
LM = LegKind.LIMIT_MAKER


def oco(*legs: ExitLeg, start: int = BASE_MS) -> list[Segment]:
    return [Segment(start, legs)]


# ---------------------------------------------------------------------- legs
def test_leg_needs_a_price_or_a_trailing_delta() -> None:
    with pytest.raises(ValueError, match="trailing delta"):
        ExitLeg(SL, None)
    with pytest.raises(ValueError, match="cannot trail"):
        ExitLeg(LM, D(105), 100)


# ---------------------------------------------------------------------- replay
async def test_trailing_take_profit_skips_quiet_minutes_and_fills_at_the_bid() -> None:
    tape = FakeTape(
        [
            (at(0), "100", False),
            (at(0, 30), "101", False),
            (at(1), "110", False),  # reaches the activation: tracking from 110
            (at(1, 30), "111", False),
            (at(2), "112", False),  # quiet: the peak rises to 113, no 1% drop
            (at(2, 30), "113", False),
            (at(3), "114", False),
            (at(3, 1), "112.8", False),  # 1% below 114: triggers (a buy print)
            (at(3, 1.1), "112.7", True),  # next print at the bid: the fill
            (at(3, 1.2), "112.5", False),
            (at(3, 1.4), "112.0", True),  # after the 250 ms window
        ]
    )
    found = await replay(
        tape, "XUSDT", oco(ExitLeg(TP, D(110), 100), ExitLeg(SL, D(95))), until_ms=at(10)
    )
    assert found == Exit("TAKE_PROFIT", at(3, 1), D("112.86"), D("112.7"), D("112.5"))
    # minutes 1 and 3 locate the trades; the execution window is fetched from the trigger on
    assert [start for start, _ in tape.trade_calls] == [at(1), at(3), at(3, 1)]


async def test_fixed_stop_fills_at_a_bid_print_or_is_marked_unconfirmed() -> None:
    legs = oco(ExitLeg(TP, D(110), 100), ExitLeg(SL, D(95)))
    sold = FakeTape([(at(0), "96", False), (at(1), "94.9", True)])
    assert await replay(sold, "XUSDT", legs, until_ms=at(5)) == Exit(
        "STOP_LOSS", at(1), D(95), D("94.9"), D("94.9")
    )
    # only a buy print: no observed sell, so the fill is the 250 ms low, flagged as an estimate
    bought = FakeTape([(at(0), "96", False), (at(1), "94.8", False), (at(1, 0.2), "94.5", False)])
    assert await replay(bought, "XUSDT", legs, until_ms=at(5)) == Exit(
        "STOP_LOSS", at(1), D(95), D("94.5"), D("94.5"), fill_confirmed=False
    )


@pytest.mark.parametrize("shift", [0, -1])
async def test_the_fill_is_read_across_the_trigger_minute(shift: float) -> None:
    """The review's case: the trigger at 59.95 s, the bid print and the low after 60 s. The
    result must not depend on where the minute boundary falls (``shift`` moves it all 1 s)."""
    tape = FakeTape(
        [
            (at(0, 10 + shift), "96", False),
            (at(0, 59.95 + shift), "94", False),  # triggers the stop at 95 (a buy print)
            (at(0, 60.05 + shift), "90", True),  # the first print at the bid: the fill
            (at(0, 60.08 + shift), "89", False),  # within 250 ms: the low
        ]
    )
    found = await replay(tape, "XUSDT", oco(ExitLeg(SL, D(95))), until_ms=at(5))
    assert found == Exit("STOP_LOSS", at(0, 59.95 + shift), D(95), D(90), D(89))


async def test_limit_maker_fills_at_its_price_after_a_print_above() -> None:
    tape = FakeTape([(at(0), "105", False), (at(1), "105.5", False)])
    found = await replay(
        tape, "XUSDT", oco(ExitLeg(LM, D(105)), ExitLeg(SL, D(95))), until_ms=at(5)
    )
    assert found == Exit("LIMIT_MAKER", at(1), None, D(105), D(105))


async def test_trailing_stop_without_a_price_tracks_from_the_first_trade() -> None:
    tape = FakeTape([(at(0), "100", False), (at(0, 1), "102", False), (at(0, 2), "99.9", True)])
    found = await replay(tape, "XUSDT", oco(ExitLeg(SL, None, 200)), until_ms=at(5))
    assert found == Exit("STOP_LOSS", at(0, 2), D("99.96"), D("99.9"), D("99.9"))


async def test_a_later_list_replaces_the_legs_and_later_lists_are_ignored() -> None:
    tape = FakeTape(
        [(at(1), "96", False), (at(3), "94", True), (at(4), "89.9", True), (at(7), "50", True)]
    )
    segments = [
        Segment(at(0), (ExitLeg(SL, D(95)),)),
        Segment(at(2), (ExitLeg(SL, D(90)),)),
        Segment(at(6), (ExitLeg(SL, D(60)),)),
    ]
    found = await replay(tape, "XUSDT", segments, until_ms=at(5))
    assert found == Exit("STOP_LOSS", at(4), D(90), D("89.9"), D("89.9"))
    assert await replay(tape, "XUSDT", segments, until_ms=at(3, 30)) is None
    assert await replay(tape, "XUSDT", segments[:1], until_ms=at(2)) is None


async def test_a_replacement_is_in_force_at_its_own_millisecond() -> None:
    """The review's case: the stop moves from 95 to 90 at 60.000 s, and 94 prints at that
    very millisecond. Segments are half-open, so the new stop applies and fires on 89."""
    tape = FakeTape([(at(0, 30), "96", False), (at(1), "94", True), (at(1, 0.1), "89", True)])
    segments = [Segment(at(0), (ExitLeg(SL, D(95)),)), Segment(at(1), (ExitLeg(SL, D(90)),))]
    found = await replay(tape, "XUSDT", segments, until_ms=at(5))
    assert found == Exit("STOP_LOSS", at(1, 0.1), D(90), D(89), D(89))


async def test_market_exit_unless_a_leg_fires_first() -> None:
    legs = oco(ExitLeg(SL, D(95)))
    tape = FakeTape([(at(0), "100", False), (at(1), "100.2", False), (at(1, 0.1), "100.1", True)])
    assert await replay(tape, "XUSDT", legs, until_ms=at(9), market_exit_ms=at(1)) == Exit(
        MARKET, at(1), None, D("100.1"), D("100.1")
    )
    silent = FakeTape([(at(0), "100", False)])
    assert await replay(silent, "XUSDT", legs, until_ms=at(9), market_exit_ms=at(1)) is None
    stopped = FakeTape([(at(0), "94", True), (at(1), "100", True)])
    found = await replay(stopped, "XUSDT", legs, until_ms=at(9), market_exit_ms=at(1))
    assert found is not None
    assert found.reason == "STOP_LOSS"


# ---------------------------------------------------------------------- positions
def intent(endpoint: str, payload: dict[str, Any], *, status: IntentStatus) -> Intent:
    moment = datetime.fromtimestamp(at(30) / 1000, UTC)
    return Intent(1, 1, IntentKind.ADJUST, endpoint, "L", payload, status, None, moment, moment)


def test_segments_come_from_the_confirmed_protection_intents() -> None:
    confirmed = IntentStatus.CONFIRMED
    intents = [
        intent(
            "opoco",
            {
                "pendingAboveType": "TAKE_PROFIT",
                "pendingAboveStopPrice": "110",
                "pendingAboveTrailingDelta": 100,
                "pendingBelowType": "STOP_LOSS",
                "pendingBelowStopPrice": "95",
            },
            status=confirmed,
        ),
        intent("oco", {"aboveType": "LIMIT_MAKER"}, status=IntentStatus.FAILED),
        intent("order", {"type": "MARKET"}, status=confirmed),
        intent(
            "oco",
            {
                "aboveType": "LIMIT_MAKER",
                "abovePrice": "120",
                "belowType": "STOP_LOSS",
                "belowTrailingDelta": 300,
            },
            status=confirmed,
        ),
    ]
    assert segments_from(intents, at(0)) == [
        Segment(at(0), (ExitLeg(TP, D(110), 100), ExitLeg(SL, D(95)))),
        Segment(at(30), (ExitLeg(LM, D(120)), ExitLeg(SL, None, 300))),
    ]


def order(order_id: int, type_: str, side: str = "SELL") -> ExchangeOrderRecord:
    return ExchangeOrderRecord(order_id=order_id, type=type_, side=side)


def fill(order_id: int, time: int, price: str, qty: str, *, buyer: bool = False) -> FillRecord:
    return FillRecord(
        order_id=order_id,
        trade_time=time,
        price=D(price),
        qty=D(qty),
        quote_qty=D(price) * D(qty),
        is_buyer=buyer,
    )


def test_demo_exit_averages_the_sell_fills_and_names_the_leg() -> None:
    orders = [order(1, "LIMIT", "BUY"), order(3, "TAKE_PROFIT")]
    fills = [
        fill(1, at(0), "100", "2", buyer=True),
        fill(3, at(5, 1), "98", "1"),
        fill(3, at(5), "96", "1"),
    ]
    assert demo_exit(orders, fills) == DemoExit("TAKE_PROFIT", at(5), D(97), D(2))
    market = demo_exit([order(4, "MARKET")], [fill(4, at(6), "99", "2")])
    assert market.reason == MARKET
    with pytest.raises(ValueError, match="without sell fills"):
        demo_exit(orders, fills[:1])


# ---------------------------------------------------------------------- report
def audit(
    position_id: int,
    demo: DemoExit,
    real: Exit | None,
    *,
    demo_trigger: Exit | None = None,
    demo_pnl: str = "-1",
) -> PositionAudit:
    return PositionAudit(position_id, "mom", "XUSDT", D(100), demo, D(demo_pnl), demo_trigger, real)


def sample() -> list[PositionAudit]:
    tp = DemoExit("TAKE_PROFIT", at(5), D(97), D(2))
    return [
        # confirmed: the same leg 2 s later
        audit(
            1,
            DemoExit("STOP_LOSS", at(5), D("94.525"), D(2)),
            Exit("STOP_LOSS", at(5, 2), D(95), D("94.905"), D("94.8")),
            demo_trigger=Exit("STOP_LOSS", at(5), D(95), D("94.525"), D("94.525")),
        ),
        # Demo-only spike: production kept the position and stopped later
        audit(
            2,
            tp,
            Exit("STOP_LOSS", at(20), D(95), D(95), D("94.9")),
            demo_trigger=Exit("TAKE_PROFIT", at(5), D("108.9"), D(97), D(97)),
            demo_pnl="-6",
        ),
        audit(3, tp, None),
        # the same leg 2 s later, but 5% better: production confirms the leg, not the fill
        audit(
            5,
            DemoExit("TAKE_PROFIT", at(5), D(97), D(2)),
            Exit("TAKE_PROFIT", at(5, 2), D("101.97"), D("101.85"), D("101.8")),
            demo_trigger=Exit("TAKE_PROFIT", at(5), D("101.97"), D(97), D(97)),
            demo_pnl="-6",
        ),
        audit(
            4,
            DemoExit(MARKET, at(9), D(101), D(1)),
            Exit(MARKET, at(9), None, D(102), D(101)),
            demo_trigger=Exit(MARKET, at(9), None, D(101), D(101)),
            demo_pnl="1",
        ),
    ]


def test_audit_reprices_the_demo_pnl_at_the_production_fill() -> None:
    confirmed, spiked, open_, better, market = sample()
    assert confirmed.confirmed
    assert confirmed.real_pnl == D("-0.24")
    assert confirmed.demo_slip == D("-0.005")
    assert confirmed.real_slip == D("-0.001")
    assert not spiked.confirmed
    assert spiked.real_pnl == D(-10)
    assert not open_.confirmed
    assert open_.real_pnl is None
    assert open_.demo_slip is None
    assert open_.real_slip is None
    assert market.confirmed
    assert market.demo_slip is None
    assert market.real_slip is None
    assert not better.confirmed
    assert better.real_pnl == D("3.7")


def test_summary_and_markdown_report() -> None:
    totals = summary(sample())
    assert totals["positions"] == 5
    assert totals["confirmed"] == 2
    assert totals["diverged"] == [2, 3, 5]
    assert totals["open_at_horizon"] == [3]
    assert totals["demo_pnl"] == D(-12)
    assert totals["real_pnl"] == D("-4.54")
    assert totals["difference"] == D("7.46")
    assert totals["demo_slip"] == "n=3, median -4.87%, worst -10.93%"
    assert totals["real_slip"] == "n=3, median -0.10%, worst -0.12%"
    assert summary([])["real_worst"] == "n=0"
    report = render(sample())
    assert "| 2 | mom | XUSDT | TAKE_PROFIT 10-10 00:05:00 | -10.93% | STOP_LOSS" in report
    assert "STOP_LOSS 10-10 00:20:00 **(diverged)**" in report
    assert "open at the horizon **(diverged)** | n/a | -1.00 | n/a |" in report
    assert (
        "PnL of the repriced positions: Demo -12.00, production -4.54 (difference +7.46)" in report
    )


def test_report_flags_fills_estimated_without_a_bid_print() -> None:
    estimated = audit(
        7,
        DemoExit("STOP_LOSS", at(5), D(94), D(1)),
        Exit("STOP_LOSS", at(5), D(95), D("94.5"), D("94.5"), fill_confirmed=False),
    )
    assert summary([estimated])["unconfirmed_fills"] == [7]
    assert summary(sample())["unconfirmed_fills"] == []
    report = render([estimated])
    assert "STOP_LOSS 10-10 00:05:00 (no bid print: fill estimated at the 250 ms low)" in report
    assert "Production fills estimated without a bid print within 60 s: [7]" in report
    assert "estimated without a bid print within 60 s: none" in render(sample())


# ---------------------------------------------------------------------- Binance tape
def kline(minute: int) -> list[Any]:
    return [at(minute), "1", "2", "0.5", "1.5", "10", at(minute) + 59_999]


def agg(trade_id: int, time: int) -> dict[str, Any]:
    return {"a": trade_id, "p": "1.5", "q": "2", "T": time, "m": True}


async def test_binance_tape_pages_klines_and_aggregate_trades() -> None:
    async with BinanceRestClient("https://fake.binance") as rest:
        tape = BinanceTape(BinanceSpotApi(rest))
        with respx.mock(base_url="https://fake.binance") as router:
            klines = router.get("/api/v3/klines").mock(
                side_effect=[
                    httpx.Response(200, json=[kline(0), kline(1)]),
                    httpx.Response(200, json=[kline(2)]),
                    httpx.Response(200, json=[]),
                ]
            )
            candles = await tape.candles("XUSDT", at(0, 30), at(2, 30))
            assert [c.open_ms for c in candles] == [at(0), at(1), at(2)]
            assert candles[0].high == D(2)
            assert candles[0].low == D("0.5")
            assert await tape.candles("XUSDT", at(9), at(9, 30)) == []
            assert klines.calls[1].request.url.params["startTime"] == str(at(2))

            first = [agg(i, at(0, i / 1000)) for i in range(1000)]
            beyond = [agg(1000, at(0, 59)), agg(1001, at(1, 1))]
            trades_route = router.get("/api/v3/aggTrades").mock(
                side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=beyond)]
            )
            trades = await tape.trades("XUSDT", at(0), at(1))
    assert [t.id for t in trades] == [*range(1000), 1000]
    assert trades[0].buyer_is_maker
    assert trades[0].price == D("1.5")
    first_query = trades_route.calls[0].request.url.params
    assert (first_query["startTime"], first_query["endTime"]) == (str(at(0)), str(at(1)))
    assert trades_route.calls[1].request.url.params["fromId"] == "1000"
