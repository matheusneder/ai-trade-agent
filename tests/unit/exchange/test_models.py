from decimal import Decimal

from trade_agent.exchange.models import (
    Account,
    BookTicker,
    CommissionRates,
    ListOrderStatus,
    Order,
    OrderList,
    OrderStatus,
    OrderType,
    Trade,
)

OTOCO_RESPONSE = {
    "orderListId": 1,
    "contingencyType": "OTO",
    "listStatusType": "EXEC_STARTED",
    "listOrderStatus": "EXECUTING",
    "listClientOrderId": "RumwQpBaDctlUu5jyG5rs0",
    "transactionTime": 1712291372842,
    "symbol": "LTCBTC",
    "orders": [
        {"symbol": "LTCBTC", "orderId": 6, "clientOrderId": "a"},
        {"symbol": "LTCBTC", "orderId": 7, "clientOrderId": "b"},
    ],
    "orderReports": [
        {
            "symbol": "LTCBTC",
            "orderId": 6,
            "orderListId": 1,
            "clientOrderId": "a",
            "transactTime": 1712291372842,
            "price": "1.00000000",
            "origQty": "1.00000000",
            "executedQty": "0.00000000",
            "cummulativeQuoteQty": "0.00000000",
            "status": "NEW",
            "timeInForce": "GTC",
            "type": "LIMIT",
            "side": "SELL",
            "workingTime": 1712291372842,
            "selfTradePreventionMode": "NONE",
        },
        {
            "symbol": "LTCBTC",
            "orderId": 7,
            "orderListId": 1,
            "clientOrderId": "b",
            "price": "0",
            "origQty": "5.00000000",
            "status": "PENDING_NEW",
            "type": "TAKE_PROFIT",
            "side": "SELL",
            "stopPrice": "6.00000000",
            "trailingDelta": 100,
        },
    ],
}


def test_order_list_parsing() -> None:
    order_list = OrderList.model_validate(OTOCO_RESPONSE)
    assert order_list.is_active
    assert order_list.list_order_status is ListOrderStatus.EXECUTING
    assert [o.order_id for o in order_list.orders] == [6, 7]
    working, pending = order_list.order_reports
    assert working.price == Decimal("1")
    assert working.status is OrderStatus.NEW
    assert pending.type is OrderType.TAKE_PROFIT
    assert pending.trailing_delta == 100
    assert pending.stop_price == Decimal("6")
    assert pending.executed_qty == 0


def test_order_list_without_reports_and_done() -> None:
    data = {k: v for k, v in OTOCO_RESPONSE.items() if k != "orderReports"}
    data["listOrderStatus"] = "ALL_DONE"
    data["listStatusType"] = "ALL_DONE"
    order_list = OrderList.model_validate(data)
    assert not order_list.is_active
    assert order_list.order_reports == ()


def test_order_status_final() -> None:
    assert OrderStatus.FILLED.is_final
    assert OrderStatus.EXPIRED.is_final
    assert not OrderStatus.NEW.is_final
    assert not OrderStatus.PARTIALLY_FILLED.is_final


def test_order_defaults_and_expiry() -> None:
    order = Order.model_validate(
        {
            "symbol": "BTCUSDT",
            "orderId": 1,
            "clientOrderId": "x",
            "origQty": "0.1",
            "status": "EXPIRED",
            "type": "LIMIT",
            "side": "BUY",
            "expiryReason": "UNFILLED_FOK_ORDER_EXPIRED",
            "fills": [],
        }
    )
    assert order.order_list_id == -1
    assert order.expiry_reason == "UNFILLED_FOK_ORDER_EXPIRED"
    assert order.time_in_force is None


def test_account_balance_lookup() -> None:
    account = Account.model_validate(
        {"canTrade": True, "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.25"}]}
    )
    assert account.balance("BTC").total == Decimal("0.75")
    missing = account.balance("ETH")
    assert missing.free == 0
    assert missing.locked == 0


def test_book_ticker_and_trade() -> None:
    ticker = BookTicker.model_validate(
        {"symbol": "BTCUSDT", "bidPrice": "1", "bidQty": "2", "askPrice": "1.01", "askQty": "3"}
    )
    assert ticker.ask_price == Decimal("1.01")
    trade = Trade.model_validate(
        {
            "symbol": "BTCUSDT",
            "id": 1,
            "orderId": 2,
            "price": "10",
            "qty": "1",
            "quoteQty": "10",
            "commission": "0.001",
            "commissionAsset": "BTC",
            "time": 3,
            "isBuyer": True,
            "isMaker": False,
        }
    )
    assert trade.commission == Decimal("0.001")
    assert trade.order_list_id == -1


def test_commission_rates_totals() -> None:
    rates = CommissionRates.model_validate(
        {
            "symbol": "BTCUSDT",
            "standardCommission": {"maker": "0.001", "taker": "0.001", "buyer": "0", "seller": "0"},
            "taxCommission": {"maker": "0.0001", "taker": "0.0002"},
            "discount": {"enabledForAccount": True},
        }
    )
    assert rates.maker_rate == Decimal("0.0011")
    assert rates.taker_rate == Decimal("0.0012")
    only_standard = CommissionRates.model_validate(
        {"symbol": "X", "standardCommission": {"maker": "0.001", "taker": "0.002"}}
    )
    assert only_standard.taker_rate == Decimal("0.002")
