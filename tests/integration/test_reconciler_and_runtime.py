"""Reconciliador e runtime (Binance simulada + PostgreSQL)."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.support.fake_binance import FakeBinance, Fault
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.models import Balance
from trade_agent.exchange.user_stream import (
    AccountPosition,
    ExecutionReport,
    ListStatusEvent,
    StreamConnected,
    UserEvent,
)
from trade_agent.execution.ids import order_ids
from trade_agent.execution.orders import EntryOrder, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.execution.positions import PositionState
from trade_agent.execution.service import PositionService
from trade_agent.persistence.db import AlreadyRunningError, Database
from trade_agent.persistence.store import IntentKind, IntentStatus, Store
from trade_agent.reconcile.reconciler import CHECKPOINT_KEY, Reconciler
from trade_agent.runtime import AgentRuntime, affected_decision

D = Decimal
S = PositionState
POLICY = ProtectionPolicy(
    TakeProfitMode.TRAILING, D("3"), StopMode.FIXED, take_profit_trailing_bips=100, stop_pct=D("4")
)


async def _no_sleep(_: float) -> None:
    return None


def _later() -> Callable[[], datetime]:
    return lambda: datetime.now(UTC) + timedelta(minutes=5)


async def _open(service: PositionService, decision: str) -> int:
    entry = EntryOrder("BTCUSDT", quantity=D("0.002"), limit_price=D("63100"))
    position = await service.open_position(
        profile="mod", entry=entry, policy=POLICY, decision_id=decision
    )
    return position.id


# ================================================================ reconciler
async def test_reconcile_all_syncs_positions_and_stores_checkpoint(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    first = await _open(service, "aaaaaaaaaa")
    second = await _open(service, "bbbbbbbbbb")
    fake.set_price("BTCUSDT", D("60000"))  # os dois stops executam
    reconciler = Reconciler(api, service, store)
    report = await reconciler.reconcile_all()
    assert report.positions == 2
    assert {t["id"] for t in report.transitions} == {first, second}
    assert all(t["to"] == "closed" for t in report.transitions)
    checkpoint = await store.get_checkpoint(CHECKPOINT_KEY)
    assert checkpoint is not None and checkpoint["positions"] == 2
    again = await reconciler.reconcile_all()
    assert again.positions == 0 and again.transitions == []


async def test_reconcile_resolves_unknown_intents(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "timeout_after"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    position_id = await _open(service, "aaaaaaaaaa")
    report = await Reconciler(api, service, store).reconcile_all()
    assert report.intents_confirmed == 1
    assert (await store.get_position(position_id)).state is S.PROTECTED


async def test_reconcile_fails_stale_intents_only_after_grace(
    service: PositionService, store: Store, api: BinanceSpotApi
) -> None:
    position_id = await _open(service, "aaaaaaaaaa")
    await store.add_intent(position_id, IntentKind.CLOSE, "order", "ta1-mod-aaaaaaaaaa-7-X", {})
    assert (await Reconciler(api, service, store).reconcile_all()).intents_failed == 0
    late = Reconciler(api, service, store, now=_later())
    assert (await late.reconcile_all()).intents_failed == 1
    [close] = [i for i in await store.intents_for(position_id) if i.kind is IntentKind.CLOSE]
    assert close.status is IntentStatus.FAILED


async def test_reconcile_detects_orphans_and_reports_errors(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    await _open(service, "aaaaaaaaaa")
    fake.free["BTC"] = D("0.002")
    ids = order_ids("mod", "cccccccccc", 3)
    await api.place_order_list(
        "oco",
        {
            "symbol": "BTCUSDT",
            "listClientOrderId": ids.list_id,
            "side": "SELL",
            "quantity": D("0.002"),
            "aboveType": "LIMIT_MAKER",
            "abovePrice": D("70000"),
            "aboveClientOrderId": ids.take_profit_id,
            "belowType": "STOP_LOSS",
            "belowStopPrice": D("50000"),
            "belowClientOrderId": ids.stop_id,
        },
    )
    fake.free["BTC"] += D("0.0002")
    await api.place_order_list(  # lista de terceiros: ignorada
        "oco",
        {
            "symbol": "BTCUSDT",
            "listClientOrderId": "web_manual",
            "side": "SELL",
            "quantity": D("0.0002"),
            "aboveType": "LIMIT_MAKER",
            "abovePrice": D("70000"),
            "belowType": "STOP_LOSS",
            "belowStopPrice": D("50000"),
        },
    )
    fake.inject(Fault("GET", "/api/v3/orderList", "reject", code=-1100, message="erro"))
    report = await Reconciler(api, service, store).reconcile_all()
    assert report.orphans == [ids.list_id]
    assert len(report.errors) == 1
    kinds = [e.kind for e in await store.recent_events(50)]
    assert "reconcile.orphan_list" in kinds and "reconcile.error" in kinds


async def test_reconcile_survives_listing_and_intent_errors(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    position_id = await _open(service, "aaaaaaaaaa")
    await store.add_intent(position_id, IntentKind.PROTECT, "oco", "ta1-mod-aaaaaaaaaa-4-L", {})
    fake.inject(Fault("GET", "/api/v3/orderList", "reject", code=-1100, message="erro"))
    fake.inject(Fault("GET", "/api/v3/openOrderList", "reject", code=-1100, message="erro"))
    report = await Reconciler(api, service, store).reconcile_all()
    assert any("intent" in e for e in report.errors)
    assert any("open_order_lists" in e for e in report.errors)


async def test_reconcile_decision(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    await _open(service, "aaaaaaaaaa")
    reconciler = Reconciler(api, service, store)
    assert await reconciler.reconcile_decision("ffffffffff") is None
    fake.set_price("BTCUSDT", D("60000"))
    closed = await reconciler.reconcile_decision("aaaaaaaaaa")
    assert closed is not None and closed.state is S.CLOSED
    assert (await reconciler.reconcile_decision("aaaaaaaaaa")) == closed


# ================================================================ runtime
def _report(**overrides: object) -> ExecutionReport:
    base: dict[str, object] = dict(
        event_time=1,
        symbol="BTCUSDT",
        client_order_id="ta1-mod-aaaaaaaaaa-0-SL",
        side="SELL",
        order_type="STOP_LOSS",
        time_in_force="GTC",
        quantity=D("0.002"),
        price=D(0),
        stop_price=D("60576"),
        order_list_id=1,
        orig_client_order_id="",
        execution_type="TRADE",
        status="FILLED",
        reject_reason="NONE",
        order_id=3,
        last_filled_qty=D("0.002"),
        cumulative_filled_qty=D("0.002"),
        last_filled_price=D("60000"),
        commission=D(0),
        commission_asset=None,
        transaction_time=1,
        trade_id=1,
        cumulative_quote_qty=D(0),
    )
    base.update(overrides)
    return ExecutionReport(**base)  # type: ignore[arg-type]


def test_affected_decision() -> None:
    assert affected_decision(_report()) == "aaaaaaaaaa"
    assert affected_decision(_report(client_order_id="web_1")) is None
    status = ListStatusEvent(
        1, "BTCUSDT", 1, "OCO", "ALL_DONE", "ALL_DONE", "NONE", "ta1-mod-bbbbbbbbbb-1-L", 1, ()
    )
    assert affected_decision(status) == "bbbbbbbbbb"
    assert (
        affected_decision(AccountPosition(1, 1, (Balance(asset="BTC", free=D(0), locked=D(0)),)))
        is None
    )


def _runtime(
    db: Database, api: BinanceSpotApi, store: Store, service: PositionService, **kw: Any
) -> AgentRuntime:
    reconciler = Reconciler(api, service, store)
    return AgentRuntime(db=db, api=api, store=store, reconciler=reconciler, **kw)


async def test_runtime_recovers_at_startup_and_reacts_to_events(
    db: Database, service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    await _open(service, "aaaaaaaaaa")
    await _open(service, "bbbbbbbbbb")
    fake.set_price("BTCUSDT", D("60000"))  # executado "com o agente desligado"
    stop = asyncio.Event()
    beats: list[int] = []

    async def heartbeat() -> None:
        beats.append(1)
        if len(beats) == 2:
            raise RuntimeError("heartbeat indisponível")  # falha em tarefa de fundo não derruba

    async def events() -> AsyncIterator[UserEvent]:
        yield StreamConnected(0, reconnected=False)
        yield _report()
        yield _report(client_order_id="web_1")
        yield StreamConnected(1, reconnected=True)
        while True:
            await asyncio.sleep(0.01)
            if len(beats) >= 3:
                stop.set()

    runtime = _runtime(
        db,
        api,
        store,
        service,
        events=events,
        reconcile_interval_s=0.01,
        heartbeat=heartbeat,
        heartbeat_interval_s=0.01,
    )
    await asyncio.wait_for(runtime.run(stop), timeout=10)
    assert await store.active_positions() == []
    kinds = [e.kind for e in reversed(await store.recent_events(200))]
    # a recuperação de partida encerra as posições antes de o agente se declarar iniciado
    assert kinds.index("position.closed") < kinds.index("agent.started")
    assert kinds[-1] == "agent.stopped"
    assert "runtime.task_failed" in kinds


async def test_runtime_refuses_second_instance(
    db: Database, postgres_url: str, service: PositionService, store: Store, api: BinanceSpotApi
) -> None:
    stop = asyncio.Event()
    first = asyncio.create_task(_runtime(db, api, store, service).run(stop))
    await asyncio.sleep(0.5)
    other_db = Database(postgres_url)
    try:
        with pytest.raises(AlreadyRunningError):
            await _runtime(other_db, api, Store(other_db), service).run(asyncio.Event())
    finally:
        stop.set()
        await first
        await other_db.dispose()


async def test_guarded_swallows_even_event_recording_failures(
    db: Database,
    service: PositionService,
    store: Store,
    api: BinanceSpotApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(db, api, store, service)

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("banco fora")

    async def failing() -> None:
        raise ValueError("x")

    monkeypatch.setattr(store, "record_event", broken)
    await runtime._guarded(failing)  # não levanta


async def test_reconciler_confirms_protection_before_service_adopts_it(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    position_id = await _open(service, "aaaaaaaaaa")
    fake.expire_list_legs(order_ids("mod", "aaaaaaaaaa").list_id, "EXCHANGE_CANCELED")
    fake.inject(Fault("POST", "/api/v3/orderList/oco", "timeout_after"))
    fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before", skip=1))
    fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    await service.sync(await store.get_position(position_id))
    report = await Reconciler(api, service, store).reconcile_all()
    assert report.intents_confirmed == 1  # resolvida antes do sync da posição
    adopted = await store.get_position(position_id)
    assert adopted.state is S.PROTECTED
    assert adopted.protection_list_id == order_ids("mod", "aaaaaaaaaa", 1).list_id
    assert len(fake.open_lists()) == 1


async def test_reconciler_confirms_exit_before_service_finalizes_it(
    service: PositionService, store: Store, api: BinanceSpotApi, fake: FakeBinance
) -> None:
    position_id = await _open(service, "aaaaaaaaaa")
    fake.inject(Fault("POST", "/api/v3/order", "timeout_after"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/order", "timeout_before"))
    await service.close_position(await store.get_position(position_id))
    report = await Reconciler(api, service, store).reconcile_all()
    assert report.intents_confirmed == 1
    assert (await store.get_position(position_id)).state is S.CLOSED
