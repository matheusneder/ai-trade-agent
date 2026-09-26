from decimal import Decimal

from trade_agent.exchange.user_stream import (
    AccountPosition,
    BalanceUpdate,
    ExecutionReport,
    ListStatusEvent,
    UnknownEvent,
    parse_user_event,
)

EXECUTION_REPORT = {
    "e": "executionReport",
    "E": 1499405658658,
    "s": "BTCUSDT",
    "c": "ta1-mod-abcdef-0-SL",
    "S": "SELL",
    "o": "STOP_LOSS",
    "f": "GTC",
    "q": "0.00999000",
    "p": "0.00000000",
    "P": "60480.00000000",
    "F": "0.00000000",
    "g": 7,
    "C": "",
    "x": "TRADE",
    "X": "FILLED",
    "r": "NONE",
    "i": 4293153,
    "l": "0.00999000",
    "z": "0.00999000",
    "L": "60470.10000000",
    "n": "0.60",
    "N": "USDT",
    "T": 1499405658657,
    "t": 99,
    "I": 8641984,
    "w": False,
    "m": False,
    "M": False,
    "O": 1499405658657,
    "Z": "604.09629900",
    "Y": "604.09629900",
    "Q": "0.00000000",
    "d": 300,
    "eR": "INSUFFICIENT_LIQUIDITY",
}


def test_parse_execution_report() -> None:
    event = parse_user_event(EXECUTION_REPORT)
    assert isinstance(event, ExecutionReport)
    assert event.client_order_id == "ta1-mod-abcdef-0-SL"
    assert event.status == "FILLED"
    assert event.stop_price == Decimal("60480")
    assert event.cumulative_filled_qty == Decimal("0.00999")
    assert event.commission_asset == "USDT"
    assert event.trailing_delta == 300
    assert event.expiry_reason == "INSUFFICIENT_LIQUIDITY"
    assert event.order_list_id == 7


def test_parse_execution_report_without_optional_fields() -> None:
    minimal = {k: v for k, v in EXECUTION_REPORT.items() if k not in {"d", "eR", "C", "N"}}
    event = parse_user_event(minimal)
    assert isinstance(event, ExecutionReport)
    assert event.trailing_delta is None
    assert event.expiry_reason is None
    assert event.orig_client_order_id == ""
    assert event.commission_asset is None


def test_parse_list_status() -> None:
    event = parse_user_event(
        {
            "e": "listStatus",
            "E": 1,
            "s": "BTCUSDT",
            "g": 2,
            "c": "OCO",
            "l": "ALL_DONE",
            "L": "ALL_DONE",
            "r": "NONE",
            "C": "ta1-mod-abcdef-0-L",
            "T": 3,
            "O": [{"s": "BTCUSDT", "i": 17, "c": "a"}, {"s": "BTCUSDT", "i": 18, "c": "b"}],
        }
    )
    assert isinstance(event, ListStatusEvent)
    assert event.orders == ((17, "a"), (18, "b"))
    assert event.list_client_order_id == "ta1-mod-abcdef-0-L"


def test_parse_account_and_balance_updates() -> None:
    position = parse_user_event(
        {"e": "outboundAccountPosition", "E": 1, "u": 2, "B": [{"a": "BTC", "f": "1", "l": "0.5"}]}
    )
    assert isinstance(position, AccountPosition)
    assert position.balances[0].total == Decimal("1.5")
    update = parse_user_event({"e": "balanceUpdate", "E": 1, "a": "USDT", "d": "-10", "T": 2})
    assert isinstance(update, BalanceUpdate)
    assert update.delta == Decimal("-10")


def test_unknown_event_is_preserved() -> None:
    event = parse_user_event({"e": "externalLockUpdate", "E": 1})
    assert isinstance(event, UnknownEvent)
    assert event.event_type == "externalLockUpdate"
    assert parse_user_event({}).event_type == ""  # type: ignore[union-attr]
