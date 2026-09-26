from decimal import Decimal

import pytest

from trade_agent.exchange.models import Trade
from trade_agent.execution.orders import ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.execution.positions import (
    ACTIVE_STATES,
    TRANSITIONS,
    FillSummary,
    InvalidTransitionError,
    PositionState,
    ensure_transition,
    merge_fees,
    net_received_base,
    policy_from_json,
    policy_to_json,
    realized_pnl,
    summarize_fills,
)

D = Decimal
S = PositionState


def _trade(qty: str, quote: str, commission: str, asset: str, buyer: bool) -> Trade:
    return Trade.model_validate(
        {
            "symbol": "BTCUSDT",
            "id": 1,
            "orderId": 1,
            "price": str(D(quote) / D(qty)),
            "qty": qty,
            "quoteQty": quote,
            "commission": commission,
            "commissionAsset": asset,
            "time": 1,
            "isBuyer": buyer,
            "isMaker": False,
        }
    )


def test_terminal_states() -> None:
    assert S.CLOSED.is_terminal
    assert S.REJECTED.is_terminal
    assert not S.PROTECTED.is_terminal
    assert S.CLOSED not in ACTIVE_STATES
    assert S.UNPROTECTED in ACTIVE_STATES
    assert set(TRANSITIONS) == set(PositionState)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PLANNED, S.ENTRY_SENT),
        (S.PLANNED, S.PROTECTED),
        (S.ENTRY_SENT, S.PARTIAL),
        (S.PROTECTED, S.ADJUSTING),
        (S.ADJUSTING, S.PROTECTED),
        (S.PROTECTED, S.UNPROTECTED),
        (S.UNPROTECTED, S.EXITING),
        (S.EXITING, S.CLOSED),
        (S.PROTECTED, S.PROTECTED),
    ],
)
def test_valid_transitions(current: PositionState, target: PositionState) -> None:
    ensure_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CLOSED, S.PROTECTED),
        (S.REJECTED, S.PLANNED),
        (S.PROTECTED, S.PLANNED),
        (S.EXITING, S.ADJUSTING),
    ],
)
def test_invalid_transitions(current: PositionState, target: PositionState) -> None:
    with pytest.raises(InvalidTransitionError) as info:
        ensure_transition(current, target)
    assert info.value.current is current
    assert info.value.target is target


@pytest.mark.parametrize(
    "policy",
    [
        ProtectionPolicy(
            TakeProfitMode.TRAILING,
            D("3"),
            StopMode.FIXED,
            take_profit_trailing_bips=100,
            stop_pct=D("4"),
        ),
        ProtectionPolicy(TakeProfitMode.LIMIT, D("5"), StopMode.TRAILING, stop_trailing_bips=250),
    ],
)
def test_policy_json_roundtrip(policy: ProtectionPolicy) -> None:
    assert policy_from_json(policy_to_json(policy)) == policy


def test_summaries_and_realized_pnl() -> None:
    entry = summarize_fills(
        [
            _trade("0.006", "378", "0.000006", "BTC", buyer=True),
            _trade("0.004", "252", "0.000004", "BTC", buyer=True),
        ]
    )
    assert entry.base_qty == D("0.010")
    assert entry.quote_qty == D("630")
    assert entry.avg_price == D("63000")
    assert net_received_base(entry, "BTC") == D("0.00999")
    exit_ = summarize_fills([_trade("0.00999", "659.34", "0.65934", "USDT", buyer=False)])
    assert exit_.avg_price == D("66000")
    pnl = realized_pnl(entry, exit_, base_asset="BTC", quote_asset="USDT")
    assert pnl == D("659.34") - D("630") - D("0.65934")
    assert merge_fees(entry, exit_) == {"BTC": "0.00001", "USDT": "0.65934"}


def test_realized_pnl_with_base_fee_on_exit_and_other_assets() -> None:
    entry = FillSummary(D("1"), D("100"), {"BNB": D("0.01")})
    exit_ = FillSummary(D("1"), D("110"), {"USDT": D("0.1"), "XYZ": D("0.001")})
    assert realized_pnl(entry, exit_, base_asset="XYZ", quote_asset="USDT") == D("9.79")
    assert FillSummary(D(0), D(0), {}).avg_price == 0
