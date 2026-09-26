from decimal import Decimal

import pytest

from trade_agent.exchange.models import ListOrderStatus, Order
from trade_agent.execution.ids import Leg, order_ids
from trade_agent.reconcile.assessment import ListSnapshot, VerdictKind, assess

IDS = order_ids("mod", "a1b2c3d4e5")


def _order(
    client_id: str,
    status: str,
    *,
    executed: str = "0",
    qty: str = "0.01",
    expiry: str | None = None,
) -> Order:
    data = {
        "symbol": "BTCUSDT",
        "orderId": abs(hash(client_id)) % 10_000,
        "clientOrderId": client_id,
        "origQty": qty,
        "executedQty": executed,
        "status": status,
        "type": "LIMIT",
        "side": "SELL",
    }
    if expiry:
        data["expiryReason"] = expiry
    return Order.model_validate(data)


def _snapshot(*orders: Order, status: ListOrderStatus = ListOrderStatus.EXECUTING) -> ListSnapshot:
    return ListSnapshot(IDS.list_id, status, orders)


def test_missing() -> None:
    assert assess(None).kind is VerdictKind.MISSING


@pytest.mark.parametrize(
    ("tp", "sl", "expected", "reason"),
    [
        ("FILLED", "EXPIRED", VerdictKind.CLOSED, "take-profit"),
        ("EXPIRED", "FILLED", VerdictKind.CLOSED, "stop"),
        ("PARTIALLY_FILLED", "EXPIRED", VerdictKind.EXITING, "parcialmente"),
        ("NEW", "NEW", VerdictKind.PROTECTED, "OCO ativo"),
        ("PENDING_NEW", "NEW", VerdictKind.ARMING, "armado"),
        ("EXPIRED", "EXPIRED", VerdictKind.UNPROTECTED, "inativas"),
        ("CANCELED", "CANCELED", VerdictKind.UNPROTECTED, "inativas"),
    ],
)
def test_oco_list_verdicts(tp: str, sl: str, expected: VerdictKind, reason: str) -> None:
    verdict = assess(_snapshot(_order(IDS.take_profit_id, tp), _order(IDS.stop_id, sl)))
    assert verdict.kind is expected
    assert reason in verdict.reason
    if expected is VerdictKind.CLOSED:
        assert verdict.exit_leg is not None
    if expected is VerdictKind.PROTECTED:
        assert verdict.protected_qty == Decimal("0.01")
    if expected is VerdictKind.UNPROTECTED:
        assert verdict.held_qty == Decimal("0.01")


@pytest.mark.parametrize(
    ("entry", "executed", "legs", "expected"),
    [
        ("NEW", "0", "PENDING_NEW", VerdictKind.AWAITING_ENTRY),
        ("PARTIALLY_FILLED", "0.004", "PENDING_NEW", VerdictKind.PARTIAL),
        ("FILLED", "0.01", "NEW", VerdictKind.PROTECTED),
        ("FILLED", "0.01", "PENDING_NEW", VerdictKind.ARMING),
        ("FILLED", "0.01", "EXPIRED", VerdictKind.UNPROTECTED),
        ("EXPIRED", "0", "EXPIRED", VerdictKind.REJECTED),
        ("CANCELED", "0.004", "CANCELED", VerdictKind.UNPROTECTED),
    ],
)
def test_opoco_list_verdicts(entry: str, executed: str, legs: str, expected: VerdictKind) -> None:
    snapshot = _snapshot(
        _order(IDS.entry_id, entry, executed=executed),
        _order(IDS.take_profit_id, legs),
        _order(IDS.stop_id, legs),
    )
    verdict = assess(snapshot)
    assert verdict.kind is expected
    assert verdict.entry is not None
    if expected is VerdictKind.PARTIAL:
        assert verdict.held_qty == Decimal("0.004")


def test_entry_filled_with_exit_executed_offline_is_closed() -> None:
    snapshot = _snapshot(
        _order(IDS.entry_id, "FILLED", executed="0.01"),
        _order(IDS.take_profit_id, "EXPIRED", expiry="OCO_TRIGGER"),
        _order(IDS.stop_id, "FILLED"),
        status=ListOrderStatus.ALL_DONE,
    )
    verdict = assess(snapshot)
    assert verdict.kind is VerdictKind.CLOSED
    assert verdict.entry is not None


def test_entry_only_or_foreign_orders() -> None:
    lone_entry = assess(_snapshot(_order(IDS.entry_id, "FILLED", executed="0.01")))
    assert lone_entry.kind is VerdictKind.UNPROTECTED
    assert lone_entry.held_qty == Decimal("0.01")
    foreign = assess(_snapshot(_order("web_123", "NEW")))
    assert foreign.kind is VerdictKind.UNPROTECTED
    assert foreign.held_qty is None
    assert _snapshot(_order("web_123", "NEW")).leg(Leg.ENTRY) is None
