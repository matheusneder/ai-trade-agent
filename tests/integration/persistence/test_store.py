from decimal import Decimal

import pytest

from trade_agent.exchange.models import Order, Trade
from trade_agent.execution.orders import EntryMode, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.execution.positions import InvalidTransitionError, PositionState
from trade_agent.persistence.store import (
    IntentKind,
    IntentStatus,
    PositionNotFoundError,
    Severity,
    Store,
    to_jsonable,
)

D = Decimal
S = PositionState
POLICY = ProtectionPolicy(
    TakeProfitMode.TRAILING, D("3"), StopMode.FIXED, take_profit_trailing_bips=100, stop_pct=D("4")
)


async def _create(store: Store, decision: str = "a1b2c3d4e5") -> int:
    position, intent = await store.create_position(
        profile="mod",
        decision_id=decision,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        entry_mode=EntryMode.LIMIT_FOK,
        policy=POLICY,
        planned_qty=D("0.01"),
        planned_price=D("63100"),
        protection_list_id=f"ta1-mod-{decision}-0-L",
        intent_endpoint="opoco",
        intent_payload={"workingPrice": D("63100.00"), "workingSide": "BUY", "n": None},
    )
    assert intent.kind is IntentKind.OPEN
    assert intent.status is IntentStatus.PENDING
    assert intent.payload == {"workingPrice": "63100", "workingSide": "BUY", "n": None}
    return position.id


def _order(order_id: int, status: str = "NEW") -> Order:
    return Order.model_validate(
        {
            "symbol": "BTCUSDT",
            "orderId": order_id,
            "orderListId": 7,
            "clientOrderId": f"ta1-mod-a1b2c3d4e5-0-{'TP' if order_id % 2 else 'SL'}",
            "origQty": "0.00999",
            "executedQty": "0",
            "status": status,
            "type": "TAKE_PROFIT",
            "side": "SELL",
            "stopPrice": "65000",
            "trailingDelta": 100,
        }
    )


def _trade(trade_id: int, buyer: bool) -> Trade:
    return Trade.model_validate(
        {
            "symbol": "BTCUSDT",
            "id": trade_id,
            "orderId": 1,
            "price": "63000",
            "qty": "0.01",
            "quoteQty": "630",
            "commission": "0.00001",
            "commissionAsset": "BTC",
            "time": 1,
            "isBuyer": buyer,
            "isMaker": False,
        }
    )


async def test_create_and_read_position(store: Store) -> None:
    position_id = await _create(store)
    position = await store.get_position(position_id)
    assert position.state is S.PLANNED
    assert position.policy == POLICY
    assert position.entry_mode is EntryMode.LIMIT_FOK
    assert position.planned_price == D("63100")
    assert position.created_at is not None
    assert (await store.find_position_by_decision("a1b2c3d4e5")) == position
    assert await store.find_position_by_decision("ffffffffff") is None
    assert [p.id for p in await store.active_positions()] == [position_id]


async def test_update_position_transitions_and_timestamps(store: Store) -> None:
    position_id = await _create(store)
    protected = await store.update_position(
        position_id,
        state=S.PROTECTED,
        protected_qty=D("0.00999"),
        policy=POLICY,
        entry_mode="limit_maker_gtc",
    )
    assert protected.opened_at is not None
    assert protected.protected_qty == D("0.00999")
    assert protected.entry_mode is EntryMode.LIMIT_MAKER_GTC
    again = await store.update_position(position_id, state=S.PROTECTED)
    assert again.opened_at == protected.opened_at
    closed = await store.update_position(position_id, state=S.CLOSED, realized_pnl=D("12.5"))
    assert closed.closed_at is not None
    assert closed.realized_pnl == D("12.5")
    assert await store.active_positions() == []
    with pytest.raises(InvalidTransitionError):
        await store.update_position(position_id, state=S.PROTECTED)


async def test_update_position_validations(store: Store) -> None:
    position_id = await _create(store)
    with pytest.raises(ValueError, match="campos desconhecidos"):
        await store.update_position(position_id, nao_existe=1)
    with pytest.raises(PositionNotFoundError):
        await store.update_position(999, state=S.CLOSED)
    with pytest.raises(PositionNotFoundError):
        await store.get_position(999)


async def test_intents_lifecycle(store: Store) -> None:
    position_id = await _create(store)
    intent = await store.add_intent(
        position_id, IntentKind.PROTECT, "oco", "ta1-mod-a1b2c3d4e5-1-L", {"quantity": D("0.00999")}
    )
    assert [i.client_id for i in await store.unresolved_intents()] == [
        "ta1-mod-a1b2c3d4e5-0-L",
        intent.client_id,
    ]
    unknown = await store.set_intent_status(intent.client_id, IntentStatus.UNKNOWN)
    assert unknown.status is IntentStatus.UNKNOWN
    failed = await store.set_intent_status(intent.client_id, IntentStatus.FAILED, "motivo")
    assert failed.error == "motivo"
    await store.set_intent_status("ta1-mod-a1b2c3d4e5-0-L", IntentStatus.CONFIRMED)
    assert await store.unresolved_intents() == []
    assert [i.kind for i in await store.intents_for(position_id)] == [
        IntentKind.OPEN,
        IntentKind.PROTECT,
    ]
    with pytest.raises(LookupError):
        await store.set_intent_status("nao-existe", IntentStatus.FAILED)
    assert (await store.get_intent(intent.client_id)).status is IntentStatus.FAILED
    with pytest.raises(LookupError):
        await store.get_intent("nao-existe")


async def test_orders_are_upserted(store: Store) -> None:
    position_id = await _create(store)
    await store.upsert_order(_order(11), position_id)
    await store.upsert_order(_order(12), position_id)
    await store.upsert_order(_order(11, "FILLED"), position_id)
    orders = await store.orders_for(position_id)
    assert [(o.order_id, o.status) for o in orders] == [(11, "FILLED"), (12, "NEW")]
    assert orders[0].trailing_delta == 100
    assert orders[0].stop_price == D("65000")


async def test_fills_are_idempotent(store: Store) -> None:
    position_id = await _create(store)
    assert await store.add_fills([_trade(1, True), _trade(2, False)], position_id) == 2
    assert await store.add_fills([_trade(2, False), _trade(3, False)], position_id) == 1
    assert await store.add_fills([], position_id) == 0
    fills = await store.fills_for(position_id)
    assert [f.trade_id for f in fills] == [1, 2, 3]
    assert fills[0].is_buyer


async def test_events_and_checkpoints(store: Store) -> None:
    position_id = await _create(store)
    await store.record_event("a", Severity.INFO)
    await store.record_event("b", Severity.CRITICAL, {"x": 1}, position_id=position_id)
    events = await store.recent_events(limit=10)
    assert [(e.kind, e.severity, e.payload) for e in events] == [
        ("b", "critical", {"x": 1}),
        ("a", "info", {}),
    ]
    assert await store.get_checkpoint("k") is None
    await store.set_checkpoint("k", {"v": 1})
    await store.set_checkpoint("k", {"v": 2})
    assert await store.get_checkpoint("k") == {"v": 2}


def test_to_jsonable() -> None:
    assert to_jsonable({"a": D("1E-5"), "b": S.CLOSED, "c": 3, "d": None}) == {
        "a": "0.00001",
        "b": "closed",
        "c": 3,
        "d": None,
    }
