"""Serviço de posições contra a Binance simulada e um PostgreSQL real."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support.clients import fake_api
from tests.support.fake_binance import FakeBinance, Fault
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.rules import OrderValidationError
from trade_agent.execution.gateway import ExecutionGateway
from trade_agent.execution.ids import order_ids
from trade_agent.execution.orders import (
    EntryMode,
    EntryOrder,
    FixedStop,
    Protection,
    ProtectionPolicy,
    StopMode,
    TakeProfitMode,
    TrailingTakeProfit,
)
from trade_agent.execution.positions import ExitReason, Position, PositionState
from trade_agent.execution.service import PositionService, RulesCache, ServiceConfig
from trade_agent.persistence.store import IntentKind, IntentStatus, Store
from trade_agent.reconcile.assessment import Verdict, VerdictKind

D = Decimal
S = PositionState
POLICY = ProtectionPolicy(
    TakeProfitMode.TRAILING, D("3"), StopMode.FIXED, take_profit_trailing_bips=100, stop_pct=D("4")
)
LIMIT_POLICY = ProtectionPolicy(TakeProfitMode.LIMIT, D("5"), StopMode.FIXED, stop_pct=D("4"))
DECISION = "a1b2c3d4e5"
IDS0 = order_ids("mod", DECISION, 0)
IDS1 = order_ids("mod", DECISION, 1)


async def _no_sleep(_: float) -> None:
    return None


def _later(minutes: int = 5) -> Callable[[], datetime]:
    return lambda: datetime.now(UTC) + timedelta(minutes=minutes)


def _service(
    api: BinanceSpotApi, store: Store, *, now: Callable[[], datetime] | None = None
) -> PositionService:
    return PositionService(
        api,
        ExecutionGateway(api, sleep=_no_sleep),
        store,
        RulesCache(api),
        config=ServiceConfig(),
        **({"now": now} if now else {}),
    )


async def _open(
    service: PositionService,
    *,
    price: str = "63100",
    mode: EntryMode = EntryMode.LIMIT_FOK,
    policy: ProtectionPolicy = POLICY,
) -> Position:
    entry = EntryOrder("BTCUSDT", quantity=D("0.01"), limit_price=D(price), mode=mode)
    return await service.open_position(
        profile="mod", entry=entry, policy=policy, decision_id=DECISION
    )


def _fail_lookups(fake: FakeBinance, path: str, *, skip: int = 0) -> None:
    """Faz as 3 consultas de confirmação do gateway falharem (após ``skip`` leituras)."""
    fake.inject(Fault("GET", path, "timeout_before", skip=skip))
    fake.inject(Fault("GET", path, "timeout_before"))
    fake.inject(Fault("GET", path, "timeout_before"))


async def _kinds(store: Store) -> list[str]:
    return [e.kind for e in reversed(await store.recent_events(200))]


# ================================================================ abertura
async def test_open_fok_becomes_protected_with_entry_fills(
    service: PositionService, store: Store, fake: FakeBinance
) -> None:
    position = await _open(service)
    assert position.state is S.PROTECTED
    assert position.entry_qty == D("0.00999")
    assert position.entry_quote == D("630")
    assert position.entry_price == D("63000")
    assert position.protected_qty == D("0.00999")
    assert position.opened_at is not None
    assert position.protection_list_id == IDS0.list_id
    [intent] = await store.intents_for(position.id)
    assert intent.status is IntentStatus.CONFIRMED
    assert len(await store.orders_for(position.id)) == 3
    assert len(await store.fills_for(position.id)) == 1
    assert await _kinds(store) == ["position.open_requested", "position.protected"]
    tp = fake.order_by_client_id(IDS0.take_profit_id)
    assert tp is not None and tp.stop_price == D("64993")  # 63100 × 1,03


async def test_open_invalid_order_touches_nothing(service: PositionService, store: Store) -> None:
    entry = EntryOrder("BTCUSDT", quantity=D("0.00001"), limit_price=D("63100"))
    with pytest.raises(OrderValidationError):
        await service.open_position(profile="mod", entry=entry, policy=POLICY)
    assert await store.active_positions() == []


async def test_open_rejected_by_exchange(store: Store) -> None:
    fake = FakeBinance(balances={"USDT": D("100")})
    async with fake_api(fake) as api:
        position = await _open(_service(api, store))
    assert position.state is S.REJECTED
    assert position.exit_reason == ExitReason.ENTRY_REJECTED
    [intent] = await store.intents_for(position.id)
    assert intent.status is IntentStatus.FAILED
    assert "insufficient balance" in (intent.error or "")


async def test_open_with_trading_disabled_is_rejected(store: Store) -> None:
    async with fake_api(FakeBinance(), trading_enabled=False) as api:
        position = await _open(_service(api, store))
    assert position.state is S.REJECTED


async def test_open_with_unknown_outcome_is_resolved_by_sync(
    service: PositionService, store: Store, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "timeout_after"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    position = await _open(service)
    assert position.state is S.PLANNED
    assert (await store.intents_for(position.id))[0].status is IntentStatus.UNKNOWN
    synced = await service.sync(position)
    assert synced.state is S.PROTECTED
    assert len(fake.lists) == 1


async def test_open_never_placed_is_rejected_after_grace(
    api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "timeout_before"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    position = await _open(_service(api, store))
    assert (await _service(api, store).sync(position)).state is S.PLANNED  # dentro da carência
    rejected = await _service(api, store, now=_later()).sync(position)
    assert rejected.state is S.REJECTED
    assert (await store.intents_for(position.id))[0].status is IntentStatus.FAILED
    assert "position.never_placed" in await _kinds(store)


async def test_missing_open_intent_is_rejected(service: PositionService, store: Store) -> None:
    position, _ = await store.create_position(
        profile="mod",
        decision_id="ffffffffff",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        entry_mode=EntryMode.LIMIT_FOK,
        policy=POLICY,
        planned_qty=D("0.01"),
        planned_price=D("63100"),
        protection_list_id="ta1-mod-ffffffffff-0-L",
        intent_endpoint="opoco",
        intent_payload={},
    )
    await store.set_intent_status("ta1-mod-ffffffffff-0-L", IntentStatus.FAILED)
    later = _service(service.api, store, now=_later())
    assert (await later.sync(position)).state is S.REJECTED


async def test_maker_entry_waits_then_protects(service: PositionService, fake: FakeBinance) -> None:
    position = await _open(service, price="62500", mode=EntryMode.LIMIT_MAKER_GTC)
    assert position.state is S.ENTRY_SENT
    assert (await service.sync(position)).state is S.ENTRY_SENT
    fake.set_price("BTCUSDT", D("62400"))
    protected = await service.sync(position)
    assert protected.state is S.PROTECTED
    assert protected.entry_price == D("62500")


async def test_partial_maker_entry(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service, price="62500", mode=EntryMode.LIMIT_MAKER_GTC)
    fake.partially_fill(IDS0.entry_id, D("0.004"))
    partial = await service.sync(position)
    assert partial.state is S.PARTIAL
    assert partial.entry_qty == D("0.004")
    assert (await service.sync(partial)).state is S.PARTIAL
    assert (await _kinds(store)).count("position.partial_entry") == 1


# ================================================================ encerramento pela exchange
async def test_take_profit_closes_with_realized_pnl(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    for price in ("65000", "66000", "65300"):
        fake.set_price("BTCUSDT", D(price))
    closed = await service.sync(position)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.TAKE_PROFIT
    assert closed.exit_price == D("65300")
    assert closed.exit_quote == D("652.347")
    assert closed.realized_pnl == D("652.347") - D("630") - D("0.652347")
    assert closed.fees == {"BTC": "0.00001", "USDT": "0.652347"}
    assert closed.closed_at is not None
    assert await service.sync(closed) is closed  # posição terminal: nada muda


async def test_stop_loss_closes_with_loss(service: PositionService, fake: FakeBinance) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("60500"))
    closed = await service.sync(position)
    assert closed.exit_reason == ExitReason.STOP_LOSS
    assert closed.realized_pnl is not None and closed.realized_pnl < 0


async def test_entry_and_exit_executed_while_offline(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service, price="62500", mode=EntryMode.LIMIT_MAKER_GTC)
    fake.set_price("BTCUSDT", D("62400"))  # entrada executa, OCO armado
    fake.set_price("BTCUSDT", D("59000"))  # stop executa
    closed = await service.sync(position)  # sincroniza direto de ENTRY_SENT
    assert closed.state is S.CLOSED
    assert closed.entry_price == D("62500")
    assert closed.exit_reason == ExitReason.STOP_LOSS


async def test_exit_in_progress_is_reported(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service, policy=LIMIT_POLICY)
    fake.partially_fill(IDS0.take_profit_id, D("0.004"))
    assert (await service.sync(position)).state is S.PROTECTED
    assert "position.exit_in_progress" in await _kinds(store)


async def test_missing_list_for_protected_position_raises_alert(
    service: PositionService, store: Store
) -> None:
    position = await _open(service)
    broken = await store.update_position(position.id, protection_list_id="ta1-mod-a1b2c3d4e5-9-L")
    assert (await service.sync(broken)).state is S.PROTECTED
    assert "protection.list_missing" in await _kinds(store)


async def test_closed_verdict_without_exit_leg_is_a_bug(service: PositionService) -> None:
    position = await _open(service)
    with pytest.raises(ValueError, match="perna de saída"):
        await service._on_closed(position, Verdict(VerdictKind.CLOSED, "x"))


# ================================================================ re-proteção
async def test_expired_legs_are_reprotected(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("63500"))
    fake.expire_list_legs(IDS0.list_id, "EXECUTION_RULE_PRICE_RANGE_EXCEEDED")
    protected = await service.sync(position)
    assert protected.state is S.PROTECTED
    assert protected.protection_list_id == IDS1.list_id
    assert protected.protection_seq == 1
    tp = fake.order_by_client_id(IDS1.take_profit_id)
    sl = fake.order_by_client_id(IDS1.stop_id)
    # política resolvida sobre o preço de entrada real (63000), não o planejado (63100)
    assert tp is not None and tp.stop_price == D("64890")
    assert sl is not None and sl.stop_price == D("60480")
    assert [ol.list_client_order_id for ol in fake.open_lists()] == [IDS1.list_id]
    kinds = await _kinds(store)
    assert "position.unprotected" in kinds and "position.reprotected" in kinds


async def test_reprotect_lifts_activation_above_current_price(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64000"))
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.set_price("BTCUSDT", D("65500"))
    await service.sync(position)
    tp = fake.order_by_client_id(IDS1.take_profit_id)
    assert tp is not None and tp.stop_price == D("65565.5")  # 65500 × 1,001


async def test_unprotected_below_stop_sells_failsafe(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.set_price("BTCUSDT", D("60000"))
    closed = await service.sync(position)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.FAILSAFE
    assert closed.exit_price == D("60000")


async def test_reprotect_rejected_falls_back_to_market_sell(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.inject(
        Fault(
            "POST",
            "/api/v3/orderList/oco",
            "reject",
            code=-2010,
            message="Order would trigger immediately.",
        )
    )
    closed = await service.sync(position)
    assert closed.exit_reason == ExitReason.FAILSAFE
    assert fake.balance("BTC") == (D("0"), D("0"))


async def test_reprotect_connection_error_keeps_unprotected(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    for _ in range(3):
        fake.inject(Fault("POST", "/api/v3/orderList/oco", "connect_error"))
    unprotected = await service.sync(position)
    assert unprotected.state is S.UNPROTECTED
    assert "protection.retry_later" in await _kinds(store)
    retried = await service.sync(unprotected)  # próxima reconciliação tenta de novo, com novo seq
    assert retried.state is S.PROTECTED
    assert retried.protection_seq == 2


async def test_reprotect_unknown_outcome_is_adopted_without_duplicate(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.inject(Fault("POST", "/api/v3/orderList/oco", "timeout_after"))
    _fail_lookups(fake, "/api/v3/orderList", skip=1)  # a 1ª leitura é o snapshot da lista 0
    unprotected = await service.sync(position)
    assert unprotected.state is S.UNPROTECTED
    adopted = await service.sync(unprotected)
    assert adopted.state is S.PROTECTED
    assert adopted.protection_list_id == IDS1.list_id
    assert len(fake.open_lists()) == 1
    intents = [i for i in await store.intents_for(position.id) if i.kind is IntentKind.PROTECT]
    assert [i.status for i in intents] == [IntentStatus.CONFIRMED]


async def test_reprotect_unknown_not_found_waits_then_retries(
    api: BinanceSpotApi, fake: FakeBinance, store: Store
) -> None:
    service = _service(api, store)
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.inject(Fault("POST", "/api/v3/orderList/oco", "timeout_before"))
    _fail_lookups(fake, "/api/v3/orderList", skip=1)
    unprotected = await service.sync(position)
    assert (await service.sync(unprotected)).state is S.UNPROTECTED  # aguarda a carência
    retried = await _service(api, store, now=_later()).sync(unprotected)
    assert retried.state is S.PROTECTED
    assert retried.protection_seq == 2


async def test_residual_balance_is_closed(service: PositionService, fake: FakeBinance) -> None:
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.free["BTC"] = D("0.00005")  # ~3 USDT: abaixo do notional mínimo
    closed = await service.sync(position)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.RESIDUAL


# ================================================================ ajuste
BREAK_EVEN = Protection(TrailingTakeProfit(D("66000"), 100), FixedStop(D("63300")))


async def test_adjust_protection_moves_stop(service: PositionService, fake: FakeBinance) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64500"))
    adjusted = await service.adjust_protection(position, BREAK_EVEN)
    assert adjusted.state is S.PROTECTED
    assert adjusted.protection_list_id == IDS1.list_id
    assert fake.order_by_client_id(IDS0.stop_id).status == "CANCELED"  # type: ignore[union-attr]
    fake.set_price("BTCUSDT", D("63250"))  # stop de break-even (+0,48%) cobre as taxas
    closed = await service.sync(adjusted)
    assert closed.exit_reason == ExitReason.STOP_LOSS
    assert closed.realized_pnl == D("631.8675") - D("630") - D("0.6318675")


async def test_adjust_requires_protected_position(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service, price="62500", mode=EntryMode.LIMIT_MAKER_GTC)
    with pytest.raises(ValueError, match="não está protegida"):
        await service.adjust_protection(position, BREAK_EVEN)


async def test_adjust_rejected_new_oco_triggers_failsafe(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64500"))
    fake.inject(Fault("POST", "/api/v3/orderList/oco", "reject", code=-2010, message="rejeitado"))
    closed = await service.adjust_protection(position, BREAK_EVEN)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.FAILSAFE


async def test_adjust_when_protection_already_executed(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("60500"))  # stop executa na exchange
    fake.set_price("BTCUSDT", D("64500"))
    closed = await service.adjust_protection(position, BREAK_EVEN)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.STOP_LOSS


async def test_adjust_unknown_outcome_is_adopted(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64500"))
    fake.inject(Fault("POST", "/api/v3/orderList/oco", "timeout_after"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    adjusting = await service.adjust_protection(position, BREAK_EVEN)
    assert adjusting.state is S.ADJUSTING
    adopted = await service.sync(adjusting)
    assert adopted.state is S.PROTECTED
    assert adopted.protection_list_id == IDS1.list_id


async def test_adjust_connection_error_after_cancel_reprotects(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64500"))
    for _ in range(3):
        fake.inject(Fault("POST", "/api/v3/orderList/oco", "connect_error"))
    result = await service.adjust_protection(position, BREAK_EVEN)
    assert result.state is S.PROTECTED
    assert result.protection_seq == 2
    assert len(fake.open_lists()) == 1


async def test_adjusting_waits_within_grace_then_reprotects(
    api: BinanceSpotApi, fake: FakeBinance, store: Store
) -> None:
    service = _service(api, store)
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64500"))
    fake.inject(Fault("POST", "/api/v3/orderList/oco", "timeout_before"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    adjusting = await service.adjust_protection(position, BREAK_EVEN)
    assert (await service.sync(adjusting)).state is S.ADJUSTING
    recovered = await _service(api, store, now=_later()).sync(adjusting)
    assert recovered.state is S.PROTECTED
    assert recovered.protection_seq == 2


# ================================================================ encerramento por decisão
async def test_close_position_sells_and_records_pnl(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("64000"))
    closed = await service.close_position(position, ExitReason.MANUAL)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.MANUAL
    assert closed.exit_price == D("64000")
    assert fake.open_lists() == []


async def test_close_when_protection_already_executed(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.set_price("BTCUSDT", D("60500"))
    closed = await service.close_position(position)
    assert closed.exit_reason == ExitReason.STOP_LOSS
    intents = await store.intents_for(position.id)
    close_intent = next(i for i in intents if i.kind is IntentKind.CLOSE)
    assert close_intent.status is IntentStatus.FAILED


async def test_close_sell_rejected_reprotects(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.inject(Fault("POST", "/api/v3/order", "reject", code=-2010, message="rejeitado"))
    result = await service.close_position(position)
    assert result.state is S.PROTECTED  # a proteção foi cancelada e recriada
    assert "exit.failed" in await _kinds(store)
    assert len(fake.open_lists()) == 1


async def test_close_unknown_outcome_then_confirmed(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service)
    fake.inject(Fault("POST", "/api/v3/order", "timeout_after"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/order", "timeout_before"))
    exiting = await service.close_position(position)
    assert exiting.state is S.EXITING
    closed = await service.sync(exiting)
    assert closed.state is S.CLOSED
    assert closed.exit_reason == ExitReason.DECISION


async def test_exiting_waits_within_grace_then_recovers(
    api: BinanceSpotApi, fake: FakeBinance, store: Store
) -> None:
    service = _service(api, store)
    position = await _open(service)
    fake.inject(Fault("POST", "/api/v3/order", "timeout_before"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/order", "timeout_before"))
    exiting = await service.close_position(position)
    assert (await service.sync(exiting)).state is S.EXITING
    recovered = await _service(api, store, now=_later()).sync(exiting)
    assert recovered.state is S.PROTECTED


async def test_exiting_with_open_exit_order_waits(
    service: PositionService, fake: FakeBinance, store: Store, api: BinanceSpotApi
) -> None:
    position = await _open(service)
    exit_id = order_ids("mod", DECISION, 5).exit_id
    fake.free["BTC"] = D("0.01")
    await api.new_order(
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": D("0.001"),
            "price": D("70000"),
            "newClientOrderId": exit_id,
        }
    )
    await store.add_intent(position.id, IntentKind.CLOSE, "order", exit_id, {})
    exiting = await store.update_position(position.id, state=S.EXITING)
    assert (await service.sync(exiting)).state is S.EXITING


async def test_failsafe_exit_confirmed_on_sync(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.set_price("BTCUSDT", D("60000"))
    fake.inject(Fault("POST", "/api/v3/order", "timeout_after"))
    _fail_lookups(fake, "/api/v3/order", skip=3)  # 3 leituras de ordens do snapshot
    exiting = await service.sync(position)
    assert exiting.state is S.EXITING
    closed = await service.sync(exiting)
    assert closed.exit_reason == ExitReason.FAILSAFE


# ================================================================ cache de regras
async def test_rules_cache_expires(api: BinanceSpotApi, fake: FakeBinance) -> None:
    ticks = iter([0.0, 10.0, 5000.0, 5000.0])
    cache = RulesCache(api, ttl_s=3600, clock=lambda: next(ticks))
    first = await cache.get("BTCUSDT")
    assert await cache.get("BTCUSDT") is first
    await cache.get("BTCUSDT")
    assert len(fake.calls("GET", "/api/v3/exchangeInfo")) == 2


# ================================================================ caminhos adicionais
async def test_fok_entry_not_filled_is_rejected(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service, price="62000")  # abaixo do mercado: FOK expira
    assert position.state is S.REJECTED
    assert position.exit_reason == ExitReason.ENTRY_REJECTED
    assert fake.balance("USDT") == (D("10000"), D("0"))


async def test_reprotect_with_limit_take_profit(
    service: PositionService, fake: FakeBinance
) -> None:
    position = await _open(service, policy=LIMIT_POLICY)
    fake.expire_list_legs(IDS0.list_id, "EXCHANGE_CANCELED")
    fake.set_price("BTCUSDT", D("66500"))  # acima do alvo original (66150)
    protected = await service.sync(position)
    assert protected.state is S.PROTECTED
    tp = fake.order_by_client_id(IDS1.take_profit_id)
    assert tp is not None and tp.type == "LIMIT_MAKER"
    assert tp.price == D("66566.5")  # 66500 × 1,001


async def test_close_with_cancel_rejected_keeps_protection(
    service: PositionService, fake: FakeBinance, store: Store
) -> None:
    position = await _open(service)
    fake.inject(Fault("DELETE", "/api/v3/orderList", "reject", code=-1100, message="erro"))
    result = await service.close_position(position)
    assert result.state is S.PROTECTED
    assert result.protection_list_id == IDS0.list_id
    assert "exit.failed" in await _kinds(store)


async def test_exiting_without_recorded_intent_is_recovered(
    service: PositionService, store: Store
) -> None:
    position = await _open(service)
    crashed = await store.update_position(position.id, state=S.EXITING)  # morreu antes da intenção
    assert (await service.sync(crashed)).state is S.PROTECTED
