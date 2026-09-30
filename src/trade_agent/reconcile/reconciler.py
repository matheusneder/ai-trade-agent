"""Reconciliação completa: intenções pendentes, posições ativas e ordens órfãs."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from trade_agent import tracing
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceError
from trade_agent.execution.ids import is_agent_id
from trade_agent.execution.positions import Position
from trade_agent.execution.service import PositionService
from trade_agent.persistence.store import Intent, IntentStatus, Severity, Store

log = structlog.get_logger(__name__)

CHECKPOINT_KEY = "reconcile"


@dataclass
class ReconcileReport:
    started_at: datetime
    finished_at: datetime | None = None
    positions: int = 0
    transitions: list[dict[str, Any]] = field(default_factory=list)
    intents_confirmed: int = 0
    intents_failed: int = 0
    orphans: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "positions": self.positions,
            "transitions": self.transitions,
            "intents_confirmed": self.intents_confirmed,
            "intents_failed": self.intents_failed,
            "orphans": self.orphans,
            "errors": self.errors,
        }


class Reconciler:
    def __init__(
        self,
        api: BinanceSpotApi,
        service: PositionService,
        store: Store,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.api = api
        self.service = service
        self.store = store
        self._now = now

    @tracing.traced("reconcile", "reconcile.all")
    async def reconcile_all(self) -> ReconcileReport:
        """Reconciliação completa (na partida, periódica e após reconexão do stream)."""
        report = ReconcileReport(started_at=self._now())
        await self._resolve_intents(report)
        positions = await self.store.active_positions()
        report.positions = len(positions)
        for position in positions:
            await self._sync(position, report)
        await self._detect_orphans(report)
        report.finished_at = self._now()
        await self.store.set_checkpoint(CHECKPOINT_KEY, report.as_dict())
        if report.transitions or report.orphans or report.errors:
            log.info("reconcile.report", **report.as_dict())
        tracing.annotate(
            positions=report.positions,
            transitions=len(report.transitions),
            orphans=len(report.orphans),
            errors=len(report.errors),
        )
        log.debug(
            "reconcile.done",
            positions=report.positions,
            transitions=len(report.transitions),
            intents_confirmed=report.intents_confirmed,
            intents_failed=report.intents_failed,
            orphans=len(report.orphans),
            errors=len(report.errors),
            elapsed_ms=round((report.finished_at - report.started_at).total_seconds() * 1000),
        )
        return report

    @tracing.traced("reconcile", "reconcile.decision")
    async def reconcile_decision(self, decision_id: str) -> Position | None:
        """Sincroniza apenas a posição de uma decisão (eventos do User Data Stream)."""
        tracing.annotate(decision=decision_id)
        position = await self.store.find_position_by_decision(decision_id)
        if position is None or position.state.is_terminal:
            return position
        return await self.service.sync(position)

    async def _sync(self, position: Position, report: ReconcileReport) -> None:
        try:
            updated = await self.service.sync(position)
        except BinanceError as exc:
            report.errors.append(f"{position.id}: {exc}")
            await self.store.record_event(
                "reconcile.error",
                Severity.HIGH,
                {"error": str(exc), "symbol": position.symbol},
                position_id=position.id,
            )
            return
        log.debug(
            "reconcile.position",
            position_id=position.id,
            symbol=position.symbol,
            before=position.state.value,
            after=updated.state.value,
        )
        if updated.state is not position.state:
            report.transitions.append(
                {"id": position.id, "from": position.state.value, "to": updated.state.value}
            )

    async def _resolve_intents(self, report: ReconcileReport) -> None:
        for intent in await self.store.unresolved_intents():
            try:
                found = await self._exists(intent)
            except BinanceError as exc:
                report.errors.append(f"intent {intent.client_id}: {exc}")
                continue
            log.debug(
                "reconcile.intent",
                client_id=intent.client_id,
                status=intent.status.value,
                found=found,
            )
            if found:
                await self.store.set_intent_status(intent.client_id, IntentStatus.CONFIRMED)
                report.intents_confirmed += 1
            elif self._now() - intent.created_at >= self.service.config.intent_grace:
                await self.store.set_intent_status(
                    intent.client_id, IntentStatus.FAILED, "não encontrada na exchange"
                )
                report.intents_failed += 1

    async def _exists(self, intent: Intent) -> bool:
        if intent.endpoint in ("opoco", "oco"):
            return await self.api.find_order_list(intent.client_id) is not None
        position = await self.store.get_position(intent.position_id)
        return await self.api.find_order(position.symbol, intent.client_id) is not None

    async def _detect_orphans(self, report: ReconcileReport) -> None:
        """Listas do agente abertas na exchange sem posição ativa correspondente (D-009)."""
        known = {p.protection_list_id for p in await self.store.active_positions()}
        try:
            open_lists = await self.api.open_order_lists()
        except BinanceError as exc:
            report.errors.append(f"open_order_lists: {exc}")
            return
        log.debug("reconcile.open_lists", total=len(open_lists), known=len(known))
        for order_list in open_lists:
            list_id = order_list.list_client_order_id
            if is_agent_id(list_id) and list_id not in known:
                report.orphans.append(list_id)
                await self.store.record_event(
                    "reconcile.orphan_list",
                    Severity.CRITICAL,
                    {"list": list_id, "symbol": order_list.symbol},
                )
