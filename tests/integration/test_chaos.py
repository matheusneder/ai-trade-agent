"""Chaos tests (plan, Phase 2): the process "dies" at critical points and restarts.

Exit criterion: no scenario results in a duplicate order or an unprotected position after
the startup reconciliation.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support.fake_binance import FakeBinance
from trade_agent.exchange.api import BinanceSpotApi, OrderListKind
from trade_agent.exchange.models import OrderList
from trade_agent.exchange.serialization import ParamValue
from trade_agent.execution.gateway import ExecutionGateway, ProtectionReplacement
from trade_agent.execution.ids import order_ids
from trade_agent.execution.orders import (
    EntryOrder,
    FixedStop,
    Protection,
    ProtectionPolicy,
    StopMode,
    TakeProfitMode,
    TrailingTakeProfit,
)
from trade_agent.execution.positions import ExitReason, PositionState
from trade_agent.execution.service import PositionService, RulesCache
from trade_agent.persistence.store import IntentStatus, Store
from trade_agent.reconcile.reconciler import Reconciler

D = Decimal
S = PositionState
DECISION = "c0ffee1234"
POLICY = ProtectionPolicy(
    TakeProfitMode.TRAILING, D("3"), StopMode.FIXED, take_profit_trailing_bips=100, stop_pct=D("4")
)
ENTRY = EntryOrder("BTCUSDT", quantity=D("0.01"), limit_price=D("63100"))


class SimulatedCrash(BaseException):
    """Process death: it is not caught by the code's ``except Exception``."""


async def _no_sleep(_: float) -> None:
    return None


class CrashingGateway(ExecutionGateway):
    def __init__(self, api: BinanceSpotApi, crash: str) -> None:
        super().__init__(api, sleep=_no_sleep)
        self.crash = crash

    async def submit_order_list(
        self, kind: OrderListKind, params: Mapping[str, ParamValue]
    ) -> OrderList:
        if self.crash == "before_send":
            raise SimulatedCrash
        result = await super().submit_order_list(kind, params)
        if self.crash == "after_send":
            raise SimulatedCrash
        return result

    async def replace_protection(
        self,
        symbol: str,
        current_list_id: str,
        new_oco_params: Mapping[str, ParamValue],
        fallback_sell_params: Mapping[str, ParamValue],
    ) -> ProtectionReplacement:
        await self.cancel_order_list(symbol, current_list_id)
        raise SimulatedCrash  # died between canceling the old OCO and creating the new one


def _service(
    api: BinanceSpotApi,
    store: Store,
    gateway: ExecutionGateway | None = None,
    now: Callable[[], datetime] | None = None,
) -> PositionService:
    gateway = gateway or ExecutionGateway(api, sleep=_no_sleep)
    if now is None:
        return PositionService(api, gateway, store, RulesCache(api))
    return PositionService(api, gateway, store, RulesCache(api), now=now)


async def _restart(api: BinanceSpotApi, store: Store, *, minutes: int = 0) -> Reconciler:
    """New instance (new services, same database), with an optionally advanced clock."""
    if not minutes:
        return Reconciler(api, _service(api, store), store)

    def now() -> datetime:
        return datetime.now(UTC) + timedelta(minutes=minutes)

    return Reconciler(api, _service(api, store, now=now), store, now=now)


async def test_crash_after_intent_before_send(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    with pytest.raises(SimulatedCrash):
        await _service(api, store, CrashingGateway(api, "before_send")).open_position(
            profile="mod", entry=ENTRY, policy=POLICY, decision_id=DECISION
        )
    [planned] = await store.active_positions()
    assert planned.state is S.PLANNED
    await (await _restart(api, store)).reconcile_all()  # within the grace period: waits
    assert (await store.get_position(planned.id)).state is S.PLANNED
    await (await _restart(api, store, minutes=5)).reconcile_all()
    assert (await store.get_position(planned.id)).state is S.REJECTED
    assert fake.lists == {}  # never sent, never resent


async def test_crash_after_send_before_recording(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    with pytest.raises(SimulatedCrash):
        await _service(api, store, CrashingGateway(api, "after_send")).open_position(
            profile="mod", entry=ENTRY, policy=POLICY, decision_id=DECISION
        )
    [planned] = await store.active_positions()
    assert planned.state is S.PLANNED
    report = await (await _restart(api, store)).reconcile_all()
    assert report.intents_confirmed == 1
    recovered = await store.get_position(planned.id)
    assert recovered.state is S.PROTECTED
    assert len(fake.lists) == 1  # no duplication
    assert (await store.intents_for(planned.id))[0].status is IntentStatus.CONFIRMED


async def test_exit_executed_while_agent_offline(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    position = await _service(api, store).open_position(
        profile="mod", entry=ENTRY, policy=POLICY, decision_id=DECISION
    )
    for price in ("65000", "67000", "66300"):  # trailing TP fills with the agent down
        fake.set_price("BTCUSDT", D(price))
    await (await _restart(api, store)).reconcile_all()
    closed = await store.get_position(position.id)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.TAKE_PROFIT
    assert closed.realized_pnl is not None and closed.realized_pnl > 0


async def test_crash_during_adjustment_is_reprotected(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    position = await _service(api, store).open_position(
        profile="mod", entry=ENTRY, policy=POLICY, decision_id=DECISION
    )
    fake.set_price("BTCUSDT", D("64500"))
    break_even = Protection(TrailingTakeProfit(D("66000"), 100), FixedStop(D("63300")))
    with pytest.raises(SimulatedCrash):
        await _service(api, store, CrashingGateway(api, "adjust")).adjust_protection(
            position, break_even
        )
    assert fake.open_lists() == []  # the position is unprotected on the exchange
    assert (await store.get_position(position.id)).state is S.ADJUSTING
    await (await _restart(api, store, minutes=5)).reconcile_all()
    recovered = await store.get_position(position.id)
    assert recovered.state is S.PROTECTED
    assert [ol.list_client_order_id for ol in fake.open_lists()] == [recovered.protection_list_id]
    assert recovered.protection_list_id == order_ids("mod", DECISION, 2).list_id


async def test_protection_expired_while_offline(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    position = await _service(api, store).open_position(
        profile="mod", entry=ENTRY, policy=POLICY, decision_id=DECISION
    )
    fake.expire_list_legs(order_ids("mod", DECISION).list_id, "EXECUTION_RULE_PRICE_RANGE_EXCEEDED")
    await (await _restart(api, store)).reconcile_all()
    assert (await store.get_position(position.id)).state is S.PROTECTED
    assert len(fake.open_lists()) == 1


async def test_repeated_restarts_are_idempotent(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    await _service(api, store).open_position(
        profile="mod", entry=ENTRY, policy=POLICY, decision_id=DECISION
    )
    for _ in range(3):
        report = await (await _restart(api, store)).reconcile_all()
        assert report.transitions == []
    assert len(fake.lists) == 1
    assert len(fake.calls("POST", "/api/v3/orderList/oco")) == 0
