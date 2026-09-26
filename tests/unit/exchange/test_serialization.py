from decimal import Decimal
from enum import Enum, StrEnum

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trade_agent.exchange.serialization import (
    clean_params,
    encode_params,
    format_decimal,
    to_param_str,
    ws_signature_payload,
)


class Side(StrEnum):
    BUY = "BUY"


class Code(Enum):
    ONE = 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1E-5"), "0.00001"),
        (Decimal("0.00001000"), "0.00001"),
        (Decimal("150.2500"), "150.25"),
        (Decimal("100"), "100"),
        (Decimal("1E+2"), "100"),
        (Decimal("0E-8"), "0"),
        (Decimal("-0.00"), "0"),
        (Decimal("-1.50"), "-1.5"),
        (Decimal("123456789.123456789"), "123456789.123456789"),
    ],
)
def test_format_decimal(value: Decimal, expected: str) -> None:
    assert format_decimal(value) == expected


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_format_decimal_rejects_non_finite(value: Decimal) -> None:
    with pytest.raises(ValueError, match="não finito"):
        format_decimal(value)


@given(
    st.decimals(min_value=Decimal("-1e12"), max_value=Decimal("1e12"), allow_nan=False, places=12)
)
def test_format_decimal_roundtrip_without_exponent(value: Decimal) -> None:
    text = format_decimal(value)
    assert "E" not in text.upper()
    assert Decimal(text) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (Side.BUY, "BUY"),
        (Code.ONE, "1"),
        (Decimal("0.10"), "0.1"),
        (42, "42"),
        ("BTCUSDT", "BTCUSDT"),
    ],
)
def test_to_param_str(value: object, expected: str) -> None:
    assert to_param_str(value) == expected  # type: ignore[arg-type]


def test_clean_params_drops_none_and_keeps_order() -> None:
    params = {"symbol": "BTCUSDT", "price": None, "quantity": Decimal("1.0"), "side": Side.BUY}
    assert list(clean_params(params).items()) == [
        ("symbol", "BTCUSDT"),
        ("quantity", "1"),
        ("side", "BUY"),
    ]


def test_encode_params_percent_encodes_non_ascii_and_reserved() -> None:
    encoded = encode_params({"symbol": "１２３４５６", "listClientOrderId": "a/b+c=d", "n": 1})
    assert encoded == (
        "symbol=%EF%BC%91%EF%BC%92%EF%BC%93%EF%BC%94%EF%BC%95%EF%BC%96"
        "&listClientOrderId=a%2Fb%2Bc%3Dd&n=1"
    )


def test_encode_params_empty() -> None:
    assert encode_params({}) == ""


def test_ws_signature_payload_sorts_and_does_not_encode() -> None:
    payload = ws_signature_payload(
        {"timestamp": 2, "symbol": "１２３", "apiKey": "k", "recvWindow": None}
    )
    assert payload == "apiKey=k&symbol=１２３&timestamp=2"
