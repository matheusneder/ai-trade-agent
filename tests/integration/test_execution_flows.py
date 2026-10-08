"""End-to-end execution flows: open → protect → adjust → close (simulated Binance)."""

from decimal import Decimal

import pytest

from tests.support.exchange_info import rules_for
from tests.support.fake_binance import FakeBinance, Fault
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceConnectionError, BinanceRejectedError
from trade_agent.exchange.models import ListOrderStatus, OrderStatus
from trade_agent.execution.gateway import ExecutionGateway, OrderOutcomeUnknownError
from trade_agent.execution.ids import order_ids
from trade_agent.execution.orders import (
    EntryMode,
    EntryOrder,
    FixedStop,
    LimitTakeProfit,
    Protection,
    TrailingStop,
    TrailingTakeProfit,
    build_market_sell,
    build_oco,
    build_opoco,
)

D = Decimal
BTC = rules_for("BTCUSDT")
IDS = order_ids("mod", "a1b2c3d4e5")
PROTECTION = Protection(
    take_profit=TrailingTakeProfit(activation_price=D("64890"), trailing_delta_bips=100),
    stop=FixedStop(stop_price=D("60480")),
)


def _entry(mode: EntryMode = EntryMode.LIMIT_FOK, price: str = "63100") -> EntryOrder:
    return EntryOrder("BTCUSDT", quantity=D("0.01"), limit_price=D(price), mode=mode)


async def _open(gateway: ExecutionGateway, **kw: object) -> None:
    await gateway.submit_order_list("opoco", build_opoco(_entry(**kw), PROTECTION, IDS, BTC))  # type: ignore[arg-type]


async def test_opoco_fok_arms_protection_with_received_quantity(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    result = await gateway.submit_order_list("opoco", build_opoco(_entry(), PROTECTION, IDS, BTC))
    assert result.is_active
    entry = fake.order_by_client_id(IDS.entry_id)
    tp = fake.order_by_client_id(IDS.take_profit_id)
    sl = fake.order_by_client_id(IDS.stop_id)
    assert entry is not None and tp is not None and sl is not None
    assert entry.status == "FILLED"
    # 0.01 bought, 0.1% fee in the base asset → 0.00999 protected (OPO semantics)
    assert tp.orig_qty == sl.orig_qty == D("0.00999")
    assert tp.is_open and sl.is_open
    assert fake.balance("BTC") == (D("0"), D("0.00999"))


async def test_trailing_take_profit_follows_the_top_and_closes(
    gateway: ExecutionGateway, fake: FakeBinance, api: BinanceSpotApi
) -> None:
    await _open(gateway)
    for price in ("64000", "64900", "66000", "67500", "67000"):
        fake.set_price("BTCUSDT", D(price))
    tp = fake.order_by_client_id(IDS.take_profit_id)
    assert tp is not None and tp.status == "NEW"  # pullback of 0.74% < 1%
    fake.set_price("BTCUSDT", D("66800"))  # pullback of 1.04% from the 67500 top
    assert tp.status == "FILLED"
    sl = fake.order_by_client_id(IDS.stop_id)
    assert sl is not None and sl.status == "EXPIRED" and sl.expiry_reason == "OCO_TRIGGER"
    order_list = await api.get_order_list(list_client_order_id=IDS.list_id)
    assert order_list.list_order_status is ListOrderStatus.ALL_DONE
    assert fake.balance("BTC") == (D("0"), D("0"))


async def test_fixed_stop_protects_on_drop(gateway: ExecutionGateway, fake: FakeBinance) -> None:
    await _open(gateway)
    fake.set_price("BTCUSDT", D("60500"))
    assert fake.order_by_client_id(IDS.stop_id).status == "NEW"  # type: ignore[union-attr]
    fake.set_price("BTCUSDT", D("60400"))
    assert fake.order_by_client_id(IDS.stop_id).status == "FILLED"  # type: ignore[union-attr]
    assert fake.order_by_client_id(IDS.take_profit_id).status == "EXPIRED"  # type: ignore[union-attr]


async def test_trailing_stop_rises_with_price(gateway: ExecutionGateway, fake: FakeBinance) -> None:
    protection = Protection(LimitTakeProfit(D("70000")), TrailingStop(300))
    await gateway.submit_order_list("opoco", build_opoco(_entry(), protection, IDS, BTC))
    fake.set_price("BTCUSDT", D("66000"))
    fake.set_price("BTCUSDT", D("64200"))  # -2.7% from the top: does not trigger
    assert fake.order_by_client_id(IDS.stop_id).status == "NEW"  # type: ignore[union-attr]
    fake.set_price("BTCUSDT", D("64000"))  # -3.03% from the 66000 top
    stop = fake.order_by_client_id(IDS.stop_id)
    assert stop is not None and stop.status == "FILLED"
    assert fake.trades[-1]["price"] == "64000"  # exit above the entry: profit locked in


async def test_fok_entry_not_fillable_expires_without_arming(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    result = await gateway.submit_order_list(
        "opoco",
        build_opoco(
            _entry(price="62000"),
            Protection(TrailingTakeProfit(D("64000"), 100), FixedStop(D("59000"))),
            IDS,
            BTC,
        ),
    )
    assert result.list_order_status is ListOrderStatus.ALL_DONE
    reports = {o.client_order_id: o for o in result.order_reports}
    assert reports[IDS.entry_id].status is OrderStatus.EXPIRED
    assert reports[IDS.entry_id].expiry_reason == "UNFILLED_FOK_ORDER_EXPIRED"
    assert reports[IDS.take_profit_id].expiry_reason == "OTO_PHASE_ONE_EXPIRED"
    assert fake.balance("USDT") == (D("10000"), D("0"))


async def test_maker_entry_arms_protection_when_filled_later(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    protection = Protection(TrailingTakeProfit(D("64000"), 100), FixedStop(D("60000")))
    entry = _entry(EntryMode.LIMIT_MAKER_GTC, price="62500")
    await gateway.submit_order_list("opoco", build_opoco(entry, protection, IDS, BTC))
    assert fake.balance("USDT") == (D("9375"), D("625"))
    tp = fake.order_by_client_id(IDS.take_profit_id)
    assert tp is not None and tp.pending
    fake.set_price("BTCUSDT", D("62400"))
    armed = fake.order_by_client_id(IDS.take_profit_id)
    assert armed is not None
    assert not armed.pending
    assert armed.is_open
    assert armed.orig_qty == D("0.00999")


async def test_duplicate_open_list_is_rejected_and_reuse_after_done_is_accepted(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    params = build_opoco(_entry(), PROTECTION, IDS, BTC)
    await gateway.submit_order_list("opoco", params)
    with pytest.raises(BinanceRejectedError, match="Duplicate"):
        await gateway.submit_order_list("opoco", params)
    fake.set_price("BTCUSDT", D("60000"))  # the stop fills; the list finishes
    # Binance accepts repeating the ID of a finished list: that is why we never resend blindly.
    again = await gateway.submit_order_list("opoco", params)
    assert again.is_active


async def test_unknown_status_after_execution_is_confirmed_without_resending(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "timeout_after"))
    result = await gateway.submit_order_list("opoco", build_opoco(_entry(), PROTECTION, IDS, BTC))
    assert result.list_client_order_id == IDS.list_id
    assert len(fake.calls("POST", "/api/v3/orderList/opoco")) == 1
    assert len(fake.lists) == 1


async def test_server_error_after_execution_is_confirmed(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/order", "server_error_after"))
    params = build_market_sell("BTCUSDT", D("0.001"), IDS.exit_id, BTC, reference_price=D("63000"))
    params["side"] = "BUY"
    order = await gateway.submit_order(params)
    assert order.status is OrderStatus.FILLED
    assert len(fake.calls("POST", "/api/v3/order")) == 1


async def test_unknown_status_not_found_raises_outcome_unknown(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "timeout_before"))
    fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))  # the 1st lookup fails too
    with pytest.raises(OrderOutcomeUnknownError) as info:
        await gateway.submit_order_list("opoco", build_opoco(_entry(), PROTECTION, IDS, BTC))
    assert info.value.client_id == IDS.list_id
    assert len(fake.calls("GET", "/api/v3/orderList")) == 3
    assert fake.lists == {}  # nothing was resent


async def test_connection_error_is_retried_safely(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "connect_error"))
    result = await gateway.submit_order_list("opoco", build_opoco(_entry(), PROTECTION, IDS, BTC))
    assert result.is_active
    assert len(fake.calls("POST", "/api/v3/orderList/opoco")) == 2


async def test_persistent_connection_error_propagates(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    for _ in range(3):
        fake.inject(Fault("POST", "/api/v3/order", "connect_error"))
    params = build_market_sell("BTCUSDT", D("0.001"), IDS.exit_id, BTC, reference_price=D("63000"))
    with pytest.raises(BinanceConnectionError):
        await gateway.submit_order(params)


async def test_replace_protection_moves_stop_to_break_even(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    await _open(gateway)
    fake.set_price("BTCUSDT", D("64500"))
    new_ids = order_ids("mod", "a1b2c3d4e5", seq=1)
    break_even = Protection(TrailingTakeProfit(D("66000"), 100), FixedStop(D("63200")))
    new_oco = build_oco(
        "BTCUSDT", D("0.00999"), break_even, new_ids, BTC, reference_price=D("64500")
    )
    fallback = build_market_sell(
        "BTCUSDT", D("0.00999"), new_ids.exit_id, BTC, reference_price=D("64500")
    )
    result = await gateway.replace_protection("BTCUSDT", IDS.list_id, new_oco, fallback)
    assert result.protection is not None and result.protection.is_active
    assert result.fallback_exit is None and not result.already_closed
    assert fake.order_by_client_id(IDS.stop_id).status == "CANCELED"  # type: ignore[union-attr]
    fake.set_price("BTCUSDT", D("63100"))
    assert fake.order_by_client_id(new_ids.stop_id).status == "FILLED"  # type: ignore[union-attr]


async def test_replace_protection_failsafe_sells_when_new_oco_rejected(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    await _open(gateway)
    new_ids = order_ids("mod", "a1b2c3d4e5", seq=1)
    new_oco = build_oco(
        "BTCUSDT", D("0.00999"), PROTECTION, new_ids, BTC, reference_price=D("63000")
    )
    fallback = build_market_sell(
        "BTCUSDT", D("0.00999"), new_ids.exit_id, BTC, reference_price=D("63000")
    )
    fake.inject(
        Fault(
            "POST",
            "/api/v3/orderList/oco",
            "reject",
            code=-2010,
            message="Order would trigger immediately.",
        )
    )
    result = await gateway.replace_protection("BTCUSDT", IDS.list_id, new_oco, fallback)
    assert result.protection is None
    assert result.fallback_exit is not None and result.fallback_exit.status is OrderStatus.FILLED
    assert fake.balance("BTC") == (D("0"), D("0"))


async def test_replace_protection_when_already_closed_sends_nothing(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    await _open(gateway)
    fake.set_price("BTCUSDT", D("60000"))  # the stop already filled on Binance
    new_ids = order_ids("mod", "a1b2c3d4e5", seq=1)
    new_oco = build_oco(
        "BTCUSDT", D("0.00999"), PROTECTION, new_ids, BTC, reference_price=D("63000")
    )
    fallback = build_market_sell(
        "BTCUSDT", D("0.00999"), new_ids.exit_id, BTC, reference_price=D("63000")
    )
    result = await gateway.replace_protection("BTCUSDT", IDS.list_id, new_oco, fallback)
    assert result.already_closed
    assert fake.calls("POST", "/api/v3/orderList/oco") == []


async def test_close_position_cancels_protection_and_sells(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    await _open(gateway)
    sell = build_market_sell("BTCUSDT", D("0.00999"), IDS.exit_id, BTC, reference_price=D("63000"))
    order = await gateway.close_position("BTCUSDT", IDS.list_id, sell)
    assert order is not None and order.status is OrderStatus.FILLED
    assert fake.open_lists() == []
    assert fake.balance("BTC") == (D("0"), D("0"))


async def test_close_position_after_protection_already_executed(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    await _open(gateway)
    fake.set_price("BTCUSDT", D("60000"))
    sell = build_market_sell("BTCUSDT", D("0.00999"), IDS.exit_id, BTC, reference_price=D("60000"))
    assert await gateway.close_position("BTCUSDT", IDS.list_id, sell) is None


async def test_close_position_without_protection(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    fake.free["BTC"] = D("0.005")
    sell = build_market_sell("BTCUSDT", D("0.005"), IDS.exit_id, BTC, reference_price=D("63000"))
    order = await gateway.close_position("BTCUSDT", None, sell)
    assert order is not None and order.status is OrderStatus.FILLED


async def test_cancel_propagates_unexpected_rejection(
    gateway: ExecutionGateway, fake: FakeBinance
) -> None:
    fake.inject(Fault("DELETE", "/api/v3/orderList", "reject", code=-1100, message="Illegal"))
    with pytest.raises(BinanceRejectedError):
        await gateway.cancel_order_list("BTCUSDT", IDS.list_id)
