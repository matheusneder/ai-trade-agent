from dataclasses import replace
from decimal import Decimal

import pytest

from tests.support.exchange_info import rules_for, symbol_data
from trade_agent.exchange.rules import OrderValidationError, SymbolRules
from trade_agent.exchange.serialization import encode_params
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
IDS = order_ids("mod", "7f3a9c2b1d")
BTC = rules_for("BTCUSDT")
TRAILING_TP = TrailingTakeProfit(activation_price=D("65000.001"), trailing_delta_bips=100)
FIXED_SL = FixedStop(stop_price=D("60000.009"))
PROTECTION = Protection(take_profit=TRAILING_TP, stop=FIXED_SL)


def _violations(exc: pytest.ExceptionInfo[OrderValidationError]) -> str:
    return " | ".join(exc.value.violations)


def test_build_opoco_fok_with_trailing_tp_and_fixed_stop() -> None:
    entry = EntryOrder("BTCUSDT", quantity=D("0.001239"), limit_price=D("63000.004"))
    params = build_opoco(entry, PROTECTION, IDS, BTC)
    assert params == {
        "symbol": "BTCUSDT",
        "listClientOrderId": "ta1-mod-7f3a9c2b1d-0-L",
        "workingType": "LIMIT",
        "workingSide": "BUY",
        "workingClientOrderId": "ta1-mod-7f3a9c2b1d-0-E",
        "workingPrice": D("63000.01"),  # FOK: rounds up
        "workingQuantity": D("0.00123"),  # quantity: down
        "workingTimeInForce": "FOK",
        "pendingSide": "SELL",
        "pendingAboveType": "TAKE_PROFIT",
        "pendingAboveStopPrice": D("65000.01"),  # activation: up
        "pendingAboveTrailingDelta": 100,
        "pendingAboveClientOrderId": "ta1-mod-7f3a9c2b1d-0-TP",
        "pendingBelowType": "STOP_LOSS",
        "pendingBelowStopPrice": D("60000.00"),  # stop: down
        "pendingBelowClientOrderId": "ta1-mod-7f3a9c2b1d-0-SL",
        "newOrderRespType": "FULL",
    }
    query = encode_params(params)
    assert "workingTimeInForce=FOK" in query
    assert "E-" not in query  # no scientific notation


def test_build_opoco_maker_with_limit_tp_and_trailing_stop() -> None:
    entry = EntryOrder("BTCUSDT", D("0.002"), D("62999.999"), mode=EntryMode.LIMIT_MAKER_GTC)
    protection = Protection(LimitTakeProfit(D("66000")), TrailingStop(300))
    params = build_opoco(entry, protection, IDS, BTC)
    assert params["workingType"] == "LIMIT_MAKER"
    assert params["workingPrice"] == D("62999.99")  # maker: rounds down
    assert params["workingTimeInForce"] is None
    assert params["pendingAboveType"] == "LIMIT_MAKER"
    assert params["pendingAbovePrice"] == D("66000")
    assert params["pendingBelowTrailingDelta"] == 300
    assert "pendingBelowStopPrice" not in params
    assert "workingTimeInForce" not in encode_params(params)


def test_build_opoco_collects_all_violations() -> None:
    entry = EntryOrder("BTCUSDT", quantity=D("0.00001"), limit_price=D("63000"))
    protection = Protection(
        TrailingTakeProfit(activation_price=D("62000"), trailing_delta_bips=5000),
        FixedStop(stop_price=D("64000")),
    )
    with pytest.raises(OrderValidationError) as info:
        build_opoco(entry, protection, IDS, BTC)
    text = _violations(info)
    assert "NOTIONAL: entrada" in text
    assert "TRAILING_DELTA" in text
    assert "ativação do take-profit deve ficar acima" in text
    assert "stop deve ficar abaixo" in text
    assert "perna de stop" in text


def test_build_opoco_limit_tp_below_entry_and_trailing_stop_out_of_bounds() -> None:
    entry = EntryOrder("BTCUSDT", D("0.001"), D("63000"))
    protection = Protection(LimitTakeProfit(D("62000")), TrailingStop(5))
    with pytest.raises(OrderValidationError) as info:
        build_opoco(entry, protection, IDS, BTC)
    text = _violations(info)
    assert "alvo do take-profit deve ficar acima" in text
    assert "TRAILING_DELTA" in text


def test_build_opoco_rejects_symbol_without_capabilities() -> None:
    data = symbol_data("BTCUSDT")
    data.update(status="BREAK", ocoAllowed=False, opoAllowed=False, orderTypes=["MARKET"])
    rules = SymbolRules.from_exchange_info(data)
    entry = EntryOrder("BTCUSDT", D("0.001"), D("63000"))
    with pytest.raises(OrderValidationError) as info:
        build_opoco(entry, PROTECTION, IDS, rules)
    text = _violations(info)
    for expected in (
        "não está em negociação",
        "não aceita OCO",
        "não aceita OPO/OTO",
        "não aceita LIMIT",
        "não aceita TAKE_PROFIT",
        "não aceita STOP_LOSS",
    ):
        assert expected in text
    maker = Protection(LimitTakeProfit(D("66000")), FIXED_SL)
    with pytest.raises(OrderValidationError, match="não aceita LIMIT_MAKER"):
        build_opoco(entry, maker, IDS, rules)


def test_trailing_stop_effective_price_drives_notional_check() -> None:
    protection = Protection(TRAILING_TP, TrailingStop(1000))
    assert protection.effective_stop_price(D("100")) == D("90")
    assert PROTECTION.effective_stop_price(D("100")) == FIXED_SL.stop_price
    tiny = EntryOrder("BTCUSDT", D("0.00008"), D("63000"))  # ~5.04 USDT entry
    with pytest.raises(OrderValidationError, match="perna de stop"):
        build_opoco(tiny, protection, IDS, BTC)


def test_build_oco_for_existing_position() -> None:
    params = build_oco(
        "BTCUSDT",
        D("0.0012399"),
        PROTECTION,
        order_ids("mod", "7f3a9c2b1d", 1),
        BTC,
        reference_price=D("63000"),
    )
    assert params == {
        "symbol": "BTCUSDT",
        "listClientOrderId": "ta1-mod-7f3a9c2b1d-1-L",
        "side": "SELL",
        "quantity": D("0.00123"),
        "aboveType": "TAKE_PROFIT",
        "aboveStopPrice": D("65000.01"),
        "aboveTrailingDelta": 100,
        "aboveClientOrderId": "ta1-mod-7f3a9c2b1d-1-TP",
        "belowType": "STOP_LOSS",
        "belowStopPrice": D("60000.00"),
        "belowClientOrderId": "ta1-mod-7f3a9c2b1d-1-SL",
        "newOrderRespType": "FULL",
    }


def test_build_oco_validates_against_reference_price() -> None:
    with pytest.raises(OrderValidationError) as info:
        build_oco("BTCUSDT", D("0"), PROTECTION, IDS, BTC, reference_price=D("66000"))
    text = _violations(info)
    assert "positiva" in text
    assert "ativação do take-profit deve ficar acima" in text


def test_build_market_sell() -> None:
    params = build_market_sell(
        "BTCUSDT", D("0.00123999"), "ta1-mod-7f3a9c2b1d-0-X", BTC, reference_price=D("63000")
    )
    assert params == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": D("0.00123"),
        "newClientOrderId": "ta1-mod-7f3a9c2b1d-0-X",
        "newOrderRespType": "FULL",
    }


def test_build_market_sell_rejects_dust_and_unsupported() -> None:
    with pytest.raises(OrderValidationError, match="NOTIONAL"):
        build_market_sell("BTCUSDT", D("0.00005"), "x", BTC, reference_price=D("63000"))
    no_market = replace(BTC, order_types=frozenset({"LIMIT"}))
    with pytest.raises(OrderValidationError, match="não aceita MARKET"):
        build_market_sell("BTCUSDT", D("0.001"), "x", no_market, reference_price=D("63000"))


def test_protection_policy_resolves_trailing_tp_and_fixed_stop() -> None:
    from trade_agent.execution.orders import ProtectionPolicy, StopMode, TakeProfitMode

    policy = ProtectionPolicy(
        take_profit_mode=TakeProfitMode.TRAILING,
        take_profit_pct=D("3"),
        take_profit_trailing_bips=100,
        stop_mode=StopMode.FIXED,
        stop_pct=D("4"),
    )
    protection = policy.resolve(D("100"))
    assert protection.take_profit == TrailingTakeProfit(D("103"), 100)
    assert protection.stop == FixedStop(D("96"))


def test_protection_policy_resolves_limit_tp_and_trailing_stop() -> None:
    from trade_agent.execution.orders import ProtectionPolicy, StopMode, TakeProfitMode

    policy = ProtectionPolicy(
        take_profit_mode=TakeProfitMode.LIMIT,
        take_profit_pct=D("5"),
        stop_mode=StopMode.TRAILING,
        stop_trailing_bips=250,
    )
    protection = policy.resolve(D("200"))
    assert protection.take_profit == LimitTakeProfit(D("210"))
    assert protection.stop == TrailingStop(250)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"take_profit_pct": D("0")}, "take_profit_pct"),
        ({"take_profit_trailing_bips": None}, "take_profit_trailing_bips"),
        ({"stop_pct": None}, "stop_pct"),
        ({"stop_pct": D("100")}, "stop_pct"),
        ({"stop_mode": "trailing", "stop_pct": None}, "stop_trailing_bips"),
    ],
)
def test_protection_policy_validation(kwargs: dict[str, object], message: str) -> None:
    from trade_agent.execution.orders import ProtectionPolicy, StopMode, TakeProfitMode

    base: dict[str, object] = {
        "take_profit_mode": TakeProfitMode.TRAILING,
        "take_profit_pct": D("3"),
        "take_profit_trailing_bips": 100,
        "stop_mode": StopMode.FIXED,
        "stop_pct": D("4"),
    }
    base.update(kwargs)
    if isinstance(base["stop_mode"], str):
        base["stop_mode"] = StopMode(base["stop_mode"])
    with pytest.raises(ValueError, match=message):
        ProtectionPolicy(**base)  # type: ignore[arg-type]
