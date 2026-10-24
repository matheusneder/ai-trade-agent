"""Demo exit audit over the real store (positions, orders, fills and intents)."""

import itertools
from decimal import Decimal
from typing import Any

from tests.support.risk import NOW, POLICY
from tests.support.tape import FakeTape, at
from trade_agent.demo_audit import MARKET, Exit, audit_position
from trade_agent.exchange.models import Order, Trade
from trade_agent.exchange.serialization import ParamValue
from trade_agent.execution.orders import EntryMode
from trade_agent.execution.positions import Position, PositionState
from trade_agent.persistence.store import IntentStatus, Store

D = Decimal
OPOCO: dict[str, ParamValue] = {
    "pendingAboveType": "TAKE_PROFIT",
    "pendingAboveStopPrice": D(110),
    "pendingAboveTrailingDelta": 100,
    "pendingBelowType": "STOP_LOSS",
    "pendingBelowStopPrice": D(95),
}
_ids = itertools.count(1)


async def demo_position(
    store: Store, *, exit_type: str, exit_ms: int, exit_price: str, pnl: str, fills: bool = True
) -> Position:
    """Closed position: entry at 100 (2 units) at minute 0, sold by ``exit_type``."""
    n = next(_ids)
    position, intent = await store.create_position(
        profile="mom",
        decision_id=f"{n:010x}",
        symbol="XUSDT",
        base_asset="X",
        quote_asset="USDT",
        entry_mode=EntryMode.LIMIT_FOK,
        policy=POLICY,
        planned_qty=D(2),
        planned_price=D(100),
        protection_list_id=f"ta1-mom-{n:010x}-0-L",
        intent_endpoint="opoco",
        intent_payload=OPOCO,
    )
    await store.set_intent_status(intent.client_id, IntentStatus.CONFIRMED)
    if fills:
        for order_id, side, type_, time, price in [
            (n * 10, "BUY", "LIMIT", at(0), "100"),
            (n * 10 + 1, "SELL", exit_type, exit_ms, exit_price),
        ]:
            await store.upsert_order(order(order_id, side, type_), position.id)
            await store.add_fills([trade(order_id, side, time, price)], position.id)
    return await store.update_position(
        position.id,
        state=PositionState.CLOSED,
        entry_price=D(100) if fills else None,
        realized_pnl=D(pnl),
        closed_at=NOW,
    )


def order(order_id: int, side: str, type_: str) -> Order:
    fields: dict[str, Any] = {
        "symbol": "XUSDT",
        "orderId": order_id,
        "clientOrderId": f"c{order_id}",
        "status": "FILLED",
        "type": type_,
        "side": side,
        "origQty": "2",
        "executedQty": "2",
    }
    return Order.model_validate(fields)


def trade(order_id: int, side: str, time: int, price: str) -> Trade:
    fields: dict[str, Any] = {
        "symbol": "XUSDT",
        "id": order_id,
        "orderId": order_id,
        "price": price,
        "qty": "2",
        "quoteQty": str(D(price) * 2),
        "commission": "0",
        "commissionAsset": "USDT",
        "time": time,
        "isBuyer": side == "BUY",
        "isMaker": False,
    }
    return Trade.model_validate(fields)


async def test_demo_only_spike_is_repriced_at_the_later_production_stop(store: Store) -> None:
    position = await demo_position(
        store, exit_type="TAKE_PROFIT", exit_ms=at(5, 1), exit_price="97", pnl="-6.2"
    )
    demo = FakeTape([(at(1), "100", False), (at(5), "111", False), (at(5, 1), "97", True)])
    real = FakeTape([(at(1), "100", False), (at(5, 1), "100", True), (at(30), "94", True)])
    audit = await audit_position(
        store, position, demo_tape=demo, real_tape=real, horizon_ms=at(60) - at(0), now_ms=at(90)
    )
    assert audit is not None
    assert (audit.demo.reason, audit.demo.price, audit.demo.qty) == ("TAKE_PROFIT", D(97), D(2))
    assert audit.demo_trigger == Exit("TAKE_PROFIT", at(5, 1), D("109.89"), D(97), D(97))
    assert audit.real == Exit("STOP_LOSS", at(30), D(95), D(94), D(94))
    assert not audit.confirmed
    assert audit.real_pnl == D("-12.2")


async def test_decision_exit_is_a_market_sell_at_the_same_moment(store: Store) -> None:
    position = await demo_position(
        store, exit_type="MARKET", exit_ms=at(8), exit_price="101", pnl="1.8"
    )
    tape = FakeTape([(at(1), "100", False), (at(8, 0.05), "101", True)])
    audit = await audit_position(
        store, position, demo_tape=tape, real_tape=tape, horizon_ms=0, now_ms=at(90)
    )
    assert audit is not None
    assert audit.demo.reason == MARKET
    assert audit.real == Exit(MARKET, at(8, 0.05), None, D(101), D(101))
    assert audit.confirmed


async def test_position_without_an_entry_is_skipped(store: Store) -> None:
    position = await demo_position(
        store, exit_type="MARKET", exit_ms=at(8), exit_price="101", pnl="0", fills=False
    )
    tape = FakeTape([])
    assert (
        await audit_position(
            store, position, demo_tape=tape, real_tape=tape, horizon_ms=0, now_ms=at(90)
        )
        is None
    )
