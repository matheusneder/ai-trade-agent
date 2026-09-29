"""Serviço de posições: abrir, sincronizar com a exchange, re-proteger, ajustar e encerrar.

Toda ação que altera a exchange segue o padrão **intenção → envio → confirmação**:

1. a intenção é gravada (com o ID de cliente determinístico) antes do envio;
2. o envio passa pelo :class:`~trade_agent.execution.gateway.ExecutionGateway`
   (confirmação de status desconhecido, sem reenvio às cegas);
3. o resultado atualiza a intenção e a posição.

O estado da posição é sempre **derivado da exchange** (:func:`sync`), nunca presumido.
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import (
    BinanceAPIError,
    BinanceConnectionError,
    TradingDisabledError,
)
from trade_agent.exchange.models import Order, OrderStatus
from trade_agent.exchange.rules import SymbolRules
from trade_agent.execution.gateway import ExecutionGateway, OrderOutcomeUnknownError
from trade_agent.execution.ids import Leg, new_decision_id, order_ids, parse_client_id
from trade_agent.execution.orders import (
    EntryOrder,
    FixedStop,
    LimitTakeProfit,
    Protection,
    ProtectionPolicy,
    TrailingTakeProfit,
    build_market_sell,
    build_oco,
    build_opoco,
)
from trade_agent.execution.positions import (
    ExitReason,
    Position,
    PositionState,
    merge_fees,
    net_received_base,
    realized_pnl,
    summarize_fills,
)
from trade_agent.persistence.store import Intent, IntentKind, IntentStatus, Severity, Store
from trade_agent.reconcile.assessment import ListSnapshot, Verdict, VerdictKind, assess

log = structlog.get_logger(__name__)

S = PositionState
BIPS = Decimal(10_000)
_SEND_FAILURES = (BinanceAPIError, BinanceConnectionError, TradingDisabledError)


class RulesCache:
    """Regras de símbolo com expiração (evita consultar ``exchangeInfo`` a cada ordem)."""

    def __init__(
        self,
        api: BinanceSpotApi,
        *,
        ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, SymbolRules]] = {}

    async def get(self, symbol: str) -> SymbolRules:
        cached = self._cache.get(symbol)
        if cached is not None and self._clock() - cached[0] < self._ttl_s:
            return cached[1]
        rules = (await self._api.exchange_info([symbol]))[symbol]
        self._cache[symbol] = (self._clock(), rules)
        return rules


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    intent_grace: timedelta = timedelta(seconds=60)
    """Tempo antes de concluir que uma intenção sem registro na exchange nunca foi aceita."""

    trigger_buffer_bips: int = 10
    """Distância mínima acima do preço atual para ativação/alvo ao re-proteger."""


class PositionService:
    def __init__(
        self,
        api: BinanceSpotApi,
        gateway: ExecutionGateway,
        store: Store,
        rules: RulesCache,
        *,
        config: ServiceConfig | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.api = api
        self.gateway = gateway
        self.store = store
        self.rules = rules
        self.config = config or ServiceConfig()
        self._now = now

    # ================================================================== abrir
    async def open_position(
        self,
        *,
        profile: str,
        entry: EntryOrder,
        policy: ProtectionPolicy,
        decision_id: str | None = None,
    ) -> Position:
        """Grava a intenção, envia o OPOCO e sincroniza o estado resultante."""
        rules = await self.rules.get(entry.symbol)
        decision = decision_id or new_decision_id()
        ids = order_ids(profile, decision)
        params = build_opoco(entry, policy.resolve(entry.limit_price), ids, rules)
        position, _ = await self.store.create_position(
            profile=profile,
            decision_id=decision,
            symbol=entry.symbol,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
            entry_mode=entry.mode,
            policy=policy,
            planned_qty=Decimal(str(params["workingQuantity"])),
            planned_price=Decimal(str(params["workingPrice"])),
            protection_list_id=ids.list_id,
            intent_endpoint="opoco",
            intent_payload=params,
        )
        await self._event("position.open_requested", Severity.INFO, position)
        try:
            await self.gateway.submit_order_list("opoco", params)
        except OrderOutcomeUnknownError:
            await self.store.set_intent_status(ids.list_id, IntentStatus.UNKNOWN)
            await self._event(
                "intent.outcome_unknown", Severity.HIGH, position, {"id": ids.list_id}
            )
            return await self.store.get_position(position.id)
        except _SEND_FAILURES as exc:
            await self.store.set_intent_status(ids.list_id, IntentStatus.FAILED, str(exc))
            position = await self.store.update_position(
                position.id, state=S.REJECTED, exit_reason=ExitReason.ENTRY_REJECTED.value
            )
            await self._event("position.rejected", Severity.HIGH, position, {"error": str(exc)})
            return position
        await self.store.set_intent_status(ids.list_id, IntentStatus.CONFIRMED)
        return await self.sync(await self.store.get_position(position.id))

    # ================================================================== sincronizar
    async def sync(self, position: Position) -> Position:
        """Deriva o estado da posição a partir da exchange e age se necessário."""
        if position.state.is_terminal:
            return position
        if position.state is S.EXITING:
            return await self._sync_exiting(position)
        if position.state is S.ADJUSTING:
            return await self._sync_adjusting(position)
        pending = await self._pending_protection(position)
        if pending is not None:
            return pending
        snapshot = await self._snapshot(position.symbol, position.protection_list_id)
        verdict = assess(snapshot)
        log.debug(
            "position.sync",
            position_id=position.id,
            symbol=position.symbol,
            state=position.state.value,
            verdict=verdict.kind.value,
            reason=verdict.reason,
        )
        return await self._apply(position, verdict, snapshot)

    async def _snapshot(self, symbol: str, list_id: str) -> ListSnapshot | None:
        order_list = await self.api.find_order_list(list_id)
        if order_list is None:
            return None
        orders = [
            await self.api.get_order(symbol, order_id=ref.order_id) for ref in order_list.orders
        ]
        return ListSnapshot(list_id, order_list.list_order_status, tuple(orders))

    async def _apply(
        self, position: Position, verdict: Verdict, snapshot: ListSnapshot | None
    ) -> Position:
        if snapshot is not None:
            for order in snapshot.orders:
                await self.store.upsert_order(order, position.id)
        kind = verdict.kind
        if kind is VerdictKind.MISSING:
            return await self._on_missing(position)
        if kind in (VerdictKind.AWAITING_ENTRY, VerdictKind.ARMING):
            if position.state is S.PLANNED:
                return await self.store.update_position(position.id, state=S.ENTRY_SENT)
            return position
        if kind is VerdictKind.PROTECTED:
            return await self._on_protected(position, verdict)
        if kind is VerdictKind.PARTIAL:
            if position.state is not S.PARTIAL:
                await self._event("position.partial_entry", Severity.HIGH, position)
            return await self.store.update_position(
                position.id, state=S.PARTIAL, entry_qty=verdict.held_qty
            )
        if kind is VerdictKind.EXITING:
            await self._event("position.exit_in_progress", Severity.INFO, position)
            return position
        if kind is VerdictKind.CLOSED:
            return await self._on_closed(position, verdict)
        if kind is VerdictKind.REJECTED:
            position = await self.store.update_position(
                position.id, state=S.REJECTED, exit_reason=ExitReason.ENTRY_REJECTED.value
            )
            await self._event(
                "position.rejected", Severity.INFO, position, {"reason": verdict.reason}
            )
            return position
        return await self._on_unprotected(position, verdict)

    async def _on_missing(self, position: Position) -> Position:
        if position.state is not S.PLANNED:
            await self._event("protection.list_missing", Severity.CRITICAL, position)
            return position
        intent = await self.store.get_intent(position.protection_list_id)
        if self._now() - intent.created_at < self.config.intent_grace:
            return position
        await self.store.set_intent_status(
            intent.client_id, IntentStatus.FAILED, "não encontrada na exchange"
        )
        position = await self.store.update_position(
            position.id, state=S.REJECTED, exit_reason=ExitReason.ENTRY_REJECTED.value
        )
        await self._event("position.never_placed", Severity.HIGH, position)
        return position

    async def _on_protected(self, position: Position, verdict: Verdict) -> Position:
        changes: dict[str, Any] = {"state": S.PROTECTED, "protected_qty": verdict.protected_qty}
        if verdict.entry is not None and position.entry_qty is None:
            changes |= await self._entry_fields(position, verdict.entry)
        was = position.state
        position = await self.store.update_position(position.id, **changes)
        if was is not S.PROTECTED:
            await self._event("position.protected", Severity.INFO, position)
        return position

    async def _entry_fields(self, position: Position, entry: Order) -> dict[str, Any]:
        trades = await self.api.my_trades(position.symbol, order_id=entry.order_id)
        await self.store.add_fills(trades, position.id)
        summary = summarize_fills(trades)
        return {
            "entry_qty": net_received_base(summary, position.base_asset),
            "entry_quote": summary.quote_qty,
            "entry_price": summary.avg_price,
        }

    async def _on_closed(self, position: Position, verdict: Verdict) -> Position:
        exit_leg = verdict.exit_leg
        if exit_leg is None:
            raise ValueError("veredito CLOSED sem perna de saída")
        parts = parse_client_id(exit_leg.client_order_id)
        reason = (
            ExitReason.TAKE_PROFIT
            if parts is not None and parts.leg is Leg.TAKE_PROFIT
            else ExitReason.STOP_LOSS
        )
        changes: dict[str, Any] = {}
        if verdict.entry is not None and position.entry_qty is None:
            changes |= await self._entry_fields(position, verdict.entry)
        return await self._finalize_exit(position, exit_leg, reason, changes)

    async def _finalize_exit(
        self,
        position: Position,
        exit_order: Order,
        reason: ExitReason,
        changes: Mapping[str, Any] | None = None,
    ) -> Position:
        await self.store.upsert_order(exit_order, position.id)
        trades = await self.api.my_trades(position.symbol, order_id=exit_order.order_id)
        await self.store.add_fills(trades, position.id)
        fills = await self.store.fills_for(position.id)
        entry = summarize_fills(f for f in fills if f.is_buyer)
        exit_ = summarize_fills(f for f in fills if not f.is_buyer)
        position = await self.store.update_position(
            position.id,
            **dict(changes or {}),
            state=S.CLOSED,
            exit_quote=exit_.quote_qty,
            exit_price=exit_.avg_price,
            realized_pnl=realized_pnl(
                entry, exit_, base_asset=position.base_asset, quote_asset=position.quote_asset
            ),
            fees=merge_fees(entry, exit_),
            exit_reason=reason.value,
        )
        await self._event(
            "position.closed",
            Severity.INFO,
            position,
            {"reason": reason.value, "pnl": str(position.realized_pnl)},
        )
        return position

    # ================================================================== re-proteção
    async def _on_unprotected(self, position: Position, verdict: Verdict) -> Position:
        position = await self.store.update_position(position.id, state=S.UNPROTECTED)
        await self._event(
            "position.unprotected", Severity.CRITICAL, position, {"reason": verdict.reason}
        )
        return await self.reprotect(position, verdict.held_qty)

    async def _pending_protection(self, position: Position) -> Position | None:
        """Adota uma re-proteção enviada cujo resultado não chegou a ser registrado.

        Evita criar um segundo OCO quando o anterior foi aceito mas a resposta se perdeu.
        """
        intent = await self._latest_intent(position, IntentKind.PROTECT)
        if (
            intent is None
            or intent.status is IntentStatus.FAILED
            or intent.client_id == position.protection_list_id
        ):
            return None
        snapshot = await self._snapshot(position.symbol, intent.client_id)
        if snapshot is None:
            if self._now() - intent.created_at < self.config.intent_grace:
                return position
            await self.store.set_intent_status(
                intent.client_id, IntentStatus.FAILED, "não encontrada na exchange"
            )
            return None
        if intent.status is not IntentStatus.CONFIRMED:
            await self.store.set_intent_status(intent.client_id, IntentStatus.CONFIRMED)
        parts = parse_client_id(intent.client_id)
        position = await self.store.update_position(
            position.id,
            protection_list_id=intent.client_id,
            protection_seq=parts.seq if parts else position.protection_seq,
        )
        return await self._apply(position, assess(snapshot), snapshot)

    def _for_current_price(self, protection: Protection, bid: Decimal) -> Protection | None:
        """Ajusta a proteção original ao preço atual; ``None`` se o stop já foi atravessado."""
        if isinstance(protection.stop, FixedStop) and bid <= protection.stop.stop_price:
            return None
        floor = bid * (1 + Decimal(self.config.trigger_buffer_bips) / BIPS)
        take_profit = protection.take_profit
        if isinstance(take_profit, TrailingTakeProfit):
            take_profit = TrailingTakeProfit(
                max(take_profit.activation_price, floor), take_profit.trailing_delta_bips
            )
        else:
            take_profit = LimitTakeProfit(max(take_profit.price, floor))
        return Protection(take_profit, protection.stop)

    async def reprotect(self, position: Position, held_qty: Decimal | None) -> Position:
        """Cria um novo OCO para o saldo sem proteção (ou vende, se o stop já foi atravessado)."""
        rules = await self.rules.get(position.symbol)
        free = (await self.api.account()).balance(position.base_asset).free
        qty = rules.round_qty(min(held_qty, free) if held_qty is not None else free)
        bid = (await self.api.book_ticker(position.symbol)).bid_price
        seq = await self._next_seq(position)
        ids = order_ids(position.profile, position.decision_id, seq)
        if qty <= 0 or rules.notional_violations(bid, qty, market=True):
            position = await self.store.update_position(
                position.id, state=S.CLOSED, exit_reason=ExitReason.RESIDUAL.value
            )
            await self._event("position.residual", Severity.HIGH, position, {"qty": str(qty)})
            return position
        reference = position.entry_price or position.planned_price
        protection = self._for_current_price(position.policy.resolve(reference), bid)
        if protection is None:
            return await self._failsafe_exit(position, qty, ids.exit_id, bid, rules)
        params = build_oco(position.symbol, qty, protection, ids, rules, reference_price=bid)
        await self.store.add_intent(position.id, IntentKind.PROTECT, "oco", ids.list_id, params)
        try:
            await self.gateway.submit_order_list("oco", params)
        except OrderOutcomeUnknownError:
            await self.store.set_intent_status(ids.list_id, IntentStatus.UNKNOWN)
            await self._event(
                "intent.outcome_unknown", Severity.HIGH, position, {"id": ids.list_id}
            )
            return position
        except BinanceConnectionError as exc:
            await self.store.set_intent_status(ids.list_id, IntentStatus.FAILED, str(exc))
            await self._event("protection.retry_later", Severity.CRITICAL, position)
            return position
        except (BinanceAPIError, TradingDisabledError) as exc:
            await self.store.set_intent_status(ids.list_id, IntentStatus.FAILED, str(exc))
            return await self._failsafe_exit(position, qty, ids.exit_id, bid, rules)
        await self.store.set_intent_status(ids.list_id, IntentStatus.CONFIRMED)
        position = await self.store.update_position(
            position.id,
            state=S.PROTECTED,
            protection_list_id=ids.list_id,
            protection_seq=seq,
            protected_qty=qty,
        )
        await self._event("position.reprotected", Severity.HIGH, position, {"list": ids.list_id})
        return position

    async def _failsafe_exit(
        self,
        position: Position,
        qty: Decimal,
        client_id: str,
        bid: Decimal,
        rules: SymbolRules,
    ) -> Position:
        params = build_market_sell(position.symbol, qty, client_id, rules, reference_price=bid)
        return await self._sell(position, params, ExitReason.FAILSAFE, IntentKind.FAILSAFE)

    async def _sell(
        self,
        position: Position,
        params: Mapping[str, Any],
        reason: ExitReason,
        kind: IntentKind,
        send: Callable[[], Awaitable[Order | None]] | None = None,
    ) -> Position:
        client_id = str(params["newClientOrderId"])
        position = await self.store.update_position(
            position.id, state=S.EXITING, exit_reason=reason.value
        )
        await self.store.add_intent(position.id, kind, "order", client_id, params)
        try:
            order = await (send() if send else self.gateway.submit_order(params))
        except OrderOutcomeUnknownError:
            await self.store.set_intent_status(client_id, IntentStatus.UNKNOWN)
            return position
        except _SEND_FAILURES as exc:
            await self.store.set_intent_status(client_id, IntentStatus.FAILED, str(exc))
            await self._event("exit.failed", Severity.CRITICAL, position, {"error": str(exc)})
            return await self._sync_exiting(position)
        if order is None:  # a proteção já havia encerrado a posição: nada foi enviado
            await self.store.set_intent_status(
                client_id, IntentStatus.FAILED, "proteção já havia encerrado a posição"
            )
            return await self._sync_exiting(position)
        await self.store.set_intent_status(client_id, IntentStatus.CONFIRMED)
        return await self._finalize_exit(position, order, reason)

    # ================================================================== encerrar
    async def close_position(
        self, position: Position, reason: ExitReason = ExitReason.DECISION
    ) -> Position:
        """Cancela a proteção e vende a mercado."""
        rules = await self.rules.get(position.symbol)
        bid = (await self.api.book_ticker(position.symbol)).bid_price
        free = (await self.api.account()).balance(position.base_asset).total
        qty = position.protected_qty or position.entry_qty or free
        exit_seq = await self._next_seq(position)
        exit_id = order_ids(position.profile, position.decision_id, exit_seq).exit_id
        params = build_market_sell(position.symbol, qty, exit_id, rules, reference_price=bid)
        list_id = position.protection_list_id
        kind = IntentKind.CLOSE
        return await self._sell(
            position,
            params,
            reason,
            kind,
            send=lambda: self.gateway.close_position(position.symbol, list_id, params),
        )

    async def _sync_exiting(self, position: Position) -> Position:
        intent = await self._latest_exit_intent(position)
        if intent is not None:
            order = await self.api.find_order(position.symbol, intent.client_id)
            if order is not None and order.status is OrderStatus.FILLED:
                await self.store.set_intent_status(intent.client_id, IntentStatus.CONFIRMED)
                reason = ExitReason(position.exit_reason or ExitReason.DECISION)
                return await self._finalize_exit(position, order, reason)
            if order is not None and not order.status.is_final:
                return position
            if intent.status is not IntentStatus.FAILED:
                if order is None and self._now() - intent.created_at < self.config.intent_grace:
                    return position
                await self.store.set_intent_status(
                    intent.client_id, IntentStatus.FAILED, "saída não executada"
                )
        snapshot = await self._snapshot(position.symbol, position.protection_list_id)
        verdict = assess(snapshot)
        if verdict.kind is VerdictKind.CLOSED:
            return await self._on_closed(position, verdict)
        if verdict.kind is VerdictKind.PROTECTED:
            return await self._on_protected(position, verdict)
        return await self._on_unprotected(position, verdict)

    # ================================================================== ajustar
    async def adjust_protection(self, position: Position, protection: Protection) -> Position:
        """Troca o OCO da posição (ex.: stop no *break-even*)."""
        if position.state is not S.PROTECTED or position.protected_qty is None:
            raise ValueError(f"posição {position.id} não está protegida ({position.state})")
        rules = await self.rules.get(position.symbol)
        bid = (await self.api.book_ticker(position.symbol)).bid_price
        seq = await self._next_seq(position)
        ids = order_ids(position.profile, position.decision_id, seq)
        qty = position.protected_qty
        oco = build_oco(position.symbol, qty, protection, ids, rules, reference_price=bid)
        fallback = build_market_sell(position.symbol, qty, ids.exit_id, rules, reference_price=bid)
        position = await self.store.update_position(position.id, state=S.ADJUSTING)
        await self.store.add_intent(position.id, IntentKind.ADJUST, "oco", ids.list_id, oco)
        try:
            result = await self.gateway.replace_protection(
                position.symbol, position.protection_list_id, oco, fallback
            )
        except OrderOutcomeUnknownError:
            await self.store.set_intent_status(ids.list_id, IntentStatus.UNKNOWN)
            return position
        except _SEND_FAILURES as exc:
            await self.store.set_intent_status(ids.list_id, IntentStatus.FAILED, str(exc))
            return await self._sync_adjusting(position)
        if result.protection is not None:
            await self.store.set_intent_status(ids.list_id, IntentStatus.CONFIRMED)
            position = await self.store.update_position(
                position.id,
                state=S.PROTECTED,
                protection_list_id=ids.list_id,
                protection_seq=seq,
                protected_qty=qty,
            )
            await self._event("protection.adjusted", Severity.INFO, position, {"list": ids.list_id})
            return position
        await self.store.set_intent_status(
            ids.list_id,
            IntentStatus.FAILED,
            "proteção anterior já encerrada" if result.already_closed else "novo OCO rejeitado",
        )
        if result.fallback_exit is not None:
            await self.store.add_intent(
                position.id, IntentKind.FAILSAFE, "order", ids.exit_id, fallback
            )
            await self.store.set_intent_status(ids.exit_id, IntentStatus.CONFIRMED)
            position = await self.store.update_position(
                position.id, state=S.EXITING, exit_reason=ExitReason.FAILSAFE.value
            )
            return await self._finalize_exit(position, result.fallback_exit, ExitReason.FAILSAFE)
        return await self._sync_adjusting(position)

    async def _sync_adjusting(self, position: Position) -> Position:
        intent = await self._latest_intent(position, IntentKind.ADJUST)
        if intent is not None and intent.status is not IntentStatus.FAILED:
            snapshot = await self._snapshot(position.symbol, intent.client_id)
            if snapshot is not None:
                await self.store.set_intent_status(intent.client_id, IntentStatus.CONFIRMED)
                parts = parse_client_id(intent.client_id)
                position = await self.store.update_position(
                    position.id,
                    protection_list_id=intent.client_id,
                    protection_seq=parts.seq if parts else position.protection_seq,
                )
                return await self._apply(position, assess(snapshot), snapshot)
            if self._now() - intent.created_at < self.config.intent_grace:
                return position
            await self.store.set_intent_status(
                intent.client_id, IntentStatus.FAILED, "não encontrada na exchange"
            )
        snapshot = await self._snapshot(position.symbol, position.protection_list_id)
        return await self._apply(position, assess(snapshot), snapshot)

    # ================================================================== utilidades
    async def _next_seq(self, position: Position) -> int:
        """Próximo ``seq`` livre: nunca reutiliza IDs de tentativas anteriores."""
        intents = await self.store.intents_for(position.id)
        parsed = (parse_client_id(intent.client_id) for intent in intents)
        return max([position.protection_seq, *(p.seq for p in parsed if p)]) + 1

    async def _latest_intent(self, position: Position, kind: IntentKind) -> Intent | None:
        intents = [i for i in await self.store.intents_for(position.id) if i.kind is kind]
        return intents[-1] if intents else None

    async def _latest_exit_intent(self, position: Position) -> Intent | None:
        intents = [
            i
            for i in await self.store.intents_for(position.id)
            if i.kind in (IntentKind.CLOSE, IntentKind.FAILSAFE)
        ]
        return intents[-1] if intents else None

    async def _event(
        self,
        kind: str,
        severity: Severity,
        position: Position,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        data = {
            "symbol": position.symbol,
            "state": position.state.value,
            "decision": position.decision_id,
            **(payload or {}),
        }
        log.info(kind, severity=severity.value, position_id=position.id, **data)
        await self.store.record_event(kind, severity, data, position_id=position.id)
