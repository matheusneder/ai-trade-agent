import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trade_agent.exchange.models import OrderSide, OrderType
from trade_agent.exchange.rules import OrderValidationError, Rounding, SymbolRules

FIXTURE = Path(__file__).parents[2] / "fixtures" / "exchange_info.json"
D = Decimal


def _symbols() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {s["symbol"]: s for s in data["symbols"]}


@pytest.fixture(scope="module")
def btc() -> SymbolRules:
    return SymbolRules.from_exchange_info(_symbols()["BTCUSDT"])


def _synthetic(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "XYZUSDT",
        "status": "TRADING",
        "baseAsset": "XYZ",
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0", "maxPrice": "0", "tickSize": "0.05"},
            {"filterType": "LOT_SIZE", "minQty": "0.1", "maxQty": "0", "stepSize": "0.1"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "50", "stepSize": "0.5"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": False},
        ],
    }
    base.update(overrides)
    return base


def test_parse_real_btcusdt(btc: SymbolRules) -> None:
    assert btc.symbol == "BTCUSDT"
    assert btc.base_asset == "BTC"
    assert btc.quote_asset == "USDT"
    assert btc.is_trading
    assert btc.tick_size == D("0.01")
    assert btc.step_size == D("0.00001")
    assert btc.market_step_size == 0
    assert btc.min_notional == D("5")
    assert btc.apply_min_to_market is True
    assert btc.opo_allowed and btc.oco_allowed and btc.oto_allowed and btc.allow_trailing_stop
    assert btc.trailing is not None
    assert (btc.trailing.min_above, btc.trailing.max_below) == (10, 2000)
    assert btc.percent_price_by_side is not None
    assert btc.max_num_algo_orders == 5
    assert btc.max_num_order_lists == 20
    assert btc.supports(OrderType.TAKE_PROFIT)
    assert btc.supports(OrderType.MARKET) is not False


def test_parse_all_fixture_symbols() -> None:
    for data in _symbols().values():
        rules = SymbolRules.from_exchange_info(data)
        assert rules.tick_size > 0
        assert rules.step_size > 0


def test_parse_legacy_min_notional_and_missing_filters() -> None:
    rules = SymbolRules.from_exchange_info(_synthetic(status="BREAK"))
    assert not rules.is_trading
    assert rules.min_notional == D("10")
    assert rules.max_notional == 0
    assert rules.apply_min_to_market is False
    assert rules.percent_price_by_side is None
    assert rules.trailing is None
    assert rules.max_num_orders is None
    assert rules.opo_allowed is False
    assert rules.market_step_size == D("0.5")


@pytest.mark.parametrize(
    ("price", "rounding", "expected"),
    [
        (D("100.004"), Rounding.DOWN, D("100.00")),
        (D("100.004"), Rounding.UP, D("100.01")),
        (D("100.005"), Rounding.NEAREST, D("100.01")),
        (D("100.0049"), Rounding.NEAREST, D("100.00")),
        (D("100.015"), Rounding.NEAREST, D("100.02")),
        (D("100.01"), Rounding.UP, D("100.01")),
    ],
)
def test_round_price(
    btc: SymbolRules, price: Decimal, rounding: Rounding, expected: Decimal
) -> None:
    assert btc.round_price(price, rounding) == expected


def test_round_price_without_tick_is_identity() -> None:
    data = _synthetic()
    data["filters"][0] = {
        "filterType": "PRICE_FILTER",
        "minPrice": "0",
        "maxPrice": "0",
        "tickSize": "0",
    }
    rules = SymbolRules.from_exchange_info(data)
    assert rules.round_price(D("1.23456")) == D("1.23456")
    assert rules.price_violations(D("1.23456")) == []


def test_round_qty(btc: SymbolRules) -> None:
    assert btc.round_qty(D("0.123456789")) == D("0.12345")
    assert btc.round_qty(D("0.000009")) == 0
    assert btc.round_qty(D("0.123456789"), market=True) == D("0.12345")


def test_round_qty_market_grid() -> None:
    rules = SymbolRules.from_exchange_info(_synthetic())
    assert rules.round_qty(D("3.37")) == D("3.3")
    assert rules.round_qty(D("3.37"), market=True) == D("3.0")
    assert rules.round_qty(D("0.7"), market=True) == 0


def test_round_qty_without_steps() -> None:
    data = _synthetic()
    data["filters"][1] = {"filterType": "LOT_SIZE", "minQty": "0", "maxQty": "0", "stepSize": "0"}
    data["filters"][2] = {
        "filterType": "MARKET_LOT_SIZE",
        "minQty": "0",
        "maxQty": "0",
        "stepSize": "0",
    }
    rules = SymbolRules.from_exchange_info(data)
    assert rules.round_qty(D("1.2345"), market=True) == D("1.2345")


def test_price_violations(btc: SymbolRules) -> None:
    assert btc.price_violations(D("65000.01")) == []
    assert any("tickSize" in v for v in btc.price_violations(D("65000.001")))
    assert any("positivo" in v for v in btc.price_violations(D("0")))
    assert any("minPrice" in v for v in btc.price_violations(D("0.001")))
    assert any("maxPrice" in v for v in btc.price_violations(D("100000000")))


def test_qty_violations(btc: SymbolRules) -> None:
    assert btc.qty_violations(D("0.001")) == []
    assert any("positiva" in v for v in btc.qty_violations(D("0")))
    assert any("minQty" in v for v in btc.qty_violations(D("0.000001")))
    assert any("maxQty" in v for v in btc.qty_violations(D("100000")))
    assert any("stepSize" in v for v in btc.qty_violations(D("0.0010001")))
    assert any("MARKET_LOT_SIZE" in v for v in btc.qty_violations(D("500"), market=True))


def test_market_qty_violations_on_market_grid() -> None:
    rules = SymbolRules.from_exchange_info(_synthetic())
    problems = rules.qty_violations(D("0.7"), market=True)
    assert any("MARKET_LOT_SIZE" in v and "minQty" in v for v in problems)
    assert any("MARKET_LOT_SIZE" in v and "stepSize" in v for v in problems)
    assert rules.qty_violations(D("1000")) == []  # LOT_SIZE without maxQty


def test_notional_violations(btc: SymbolRules) -> None:
    assert btc.notional_violations(D("100"), D("0.1")) == []
    assert any("minNotional" in v for v in btc.notional_violations(D("100"), D("0.01")))
    assert any("maxNotional" in v for v in btc.notional_violations(D("100000"), D("100000")))
    # applyMaxToMarket=false on BTCUSDT: market orders ignore the maximum
    assert btc.notional_violations(D("100000"), D("100000"), market=True) == []


def test_notional_market_respects_apply_min_flag() -> None:
    rules = SymbolRules.from_exchange_info(_synthetic())
    assert rules.notional_violations(D("1"), D("1"), market=True) == []
    assert rules.notional_violations(D("1"), D("1")) != []


@pytest.mark.parametrize(
    ("order_type", "side", "delta", "ok"),
    [
        (OrderType.TAKE_PROFIT, OrderSide.SELL, 100, True),
        (OrderType.STOP_LOSS, OrderSide.SELL, 2000, True),
        (OrderType.STOP_LOSS, OrderSide.SELL, 2001, False),
        (OrderType.STOP_LOSS_LIMIT, OrderSide.BUY, 5, False),
        (OrderType.TAKE_PROFIT_LIMIT, OrderSide.SELL, 10, True),
    ],
)
def test_trailing_violations(
    btc: SymbolRules, order_type: OrderType, side: OrderSide, delta: int, ok: bool
) -> None:
    assert (btc.trailing_violations(delta, side=side, order_type=order_type) == []) is ok


def test_trailing_not_allowed_or_unbounded() -> None:
    rules = SymbolRules.from_exchange_info(_synthetic())
    assert rules.trailing_violations(100, side=OrderSide.SELL, order_type=OrderType.STOP_LOSS)
    rules = SymbolRules.from_exchange_info(_synthetic(allowTrailingStop=True))
    assert rules.trailing_violations(1, side=OrderSide.SELL, order_type=OrderType.STOP_LOSS) == []


def test_percent_price_violations(btc: SymbolRules) -> None:
    avg = D("60000")
    assert btc.percent_price_violations(D("60000"), side=OrderSide.BUY, avg_price=avg) == []
    assert btc.percent_price_violations(D("20000"), side=OrderSide.BUY, avg_price=avg)
    assert btc.percent_price_violations(D("40000"), side=OrderSide.SELL, avg_price=avg)
    rules = SymbolRules.from_exchange_info(_synthetic())
    assert rules.percent_price_violations(D("1"), side=OrderSide.SELL, avg_price=avg) == []


def test_order_validation_error_joins_violations() -> None:
    error = OrderValidationError(["a", "b"])
    assert str(error) == "a; b"
    assert error.violations == ["a", "b"]


@given(st.decimals(min_value=D("0.01"), max_value=D("999999"), places=6))
def test_rounded_prices_always_pass_price_filter(price: Decimal) -> None:
    rules = SymbolRules.from_exchange_info(_symbols()["BTCUSDT"])
    for rounding in Rounding:
        rounded = rules.round_price(price, rounding)
        if rounded >= rules.min_price:
            assert rules.price_violations(rounded) == []
    assert rules.round_price(price, Rounding.DOWN) <= price <= rules.round_price(price, Rounding.UP)


@given(st.decimals(min_value=D("0.00001"), max_value=D("9000"), places=9))
def test_rounded_qty_never_exceeds_input_and_is_valid(qty: Decimal) -> None:
    rules = SymbolRules.from_exchange_info(_symbols()["BTCUSDT"])
    rounded = rules.round_qty(qty)
    assert rounded <= qty
    assert qty - rounded < rules.step_size
    assert rules.qty_violations(rounded) == []
