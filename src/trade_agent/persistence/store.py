"""Agent repository: positions, intents, order mirror, fills and events."""

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from trade_agent.exchange.models import Order, Trade
from trade_agent.exchange.serialization import ParamValue, format_decimal
from trade_agent.execution.orders import EntryMode, ProtectionPolicy
from trade_agent.execution.positions import (
    ACTIVE_STATES,
    Position,
    PositionState,
    ensure_transition,
    policy_from_json,
    policy_to_json,
)
from trade_agent.persistence.db import Database
from trade_agent.persistence.models import (
    CheckpointRecord,
    EventRecord,
    ExchangeOrderRecord,
    FillRecord,
    IntentRecord,
    PositionRecord,
)


class IntentKind(StrEnum):
    OPEN = "open"
    PROTECT = "protect"
    ADJUST = "adjust"
    CLOSE = "close"
    FAILSAFE = "failsafe"


class IntentStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    INFO = "info"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Intent:
    id: int
    position_id: int
    kind: IntentKind
    endpoint: str
    client_id: str
    payload: dict[str, Any]
    status: IntentStatus
    error: str | None
    created_at: datetime
    updated_at: datetime


class PositionNotFoundError(LookupError):
    pass


def to_jsonable(params: Mapping[str, ParamValue]) -> dict[str, Any]:
    """Order parameters as JSON (Decimal → text without an exponent, Enum → value)."""
    result: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, Decimal):
            result[key] = format_decimal(value)
        elif isinstance(value, Enum):
            result[key] = value.value
        else:
            result[key] = value
    return result


_POSITION_FIELDS = frozenset(f.name for f in dataclasses.fields(Position)) - {
    "id",
    "created_at",
    "updated_at",
}


def _to_position(record: PositionRecord) -> Position:
    return Position(
        id=record.id,
        profile=record.profile,
        decision_id=record.decision_id,
        symbol=record.symbol,
        base_asset=record.base_asset,
        quote_asset=record.quote_asset,
        state=PositionState(record.state),
        entry_mode=EntryMode(record.entry_mode),
        policy=policy_from_json(record.policy),
        planned_qty=record.planned_qty,
        planned_price=record.planned_price,
        protection_list_id=record.protection_list_id,
        protection_seq=record.protection_seq,
        entry_qty=record.entry_qty,
        entry_quote=record.entry_quote,
        entry_price=record.entry_price,
        protected_qty=record.protected_qty,
        exit_quote=record.exit_quote,
        exit_price=record.exit_price,
        realized_pnl=record.realized_pnl,
        exit_reason=record.exit_reason,
        fees=dict(record.fees or {}),
        opened_at=record.opened_at,
        closed_at=record.closed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_intent(record: IntentRecord) -> Intent:
    return Intent(
        id=record.id,
        position_id=record.position_id,
        kind=IntentKind(record.kind),
        endpoint=record.endpoint,
        client_id=record.client_id,
        payload=dict(record.payload),
        status=IntentStatus(record.status),
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ================================================================== positions
    async def create_position(
        self,
        *,
        profile: str,
        decision_id: str,
        symbol: str,
        base_asset: str,
        quote_asset: str,
        entry_mode: EntryMode,
        policy: ProtectionPolicy,
        planned_qty: Decimal,
        planned_price: Decimal,
        protection_list_id: str,
        intent_endpoint: str,
        intent_payload: Mapping[str, ParamValue],
    ) -> tuple[Position, Intent]:
        """Creates the ``PLANNED`` position and the opening intent in the same transaction."""
        async with self.db.session() as session:
            record = PositionRecord(
                profile=profile,
                decision_id=decision_id,
                symbol=symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                state=PositionState.PLANNED.value,
                entry_mode=entry_mode.value,
                policy=policy_to_json(policy),
                planned_qty=planned_qty,
                planned_price=planned_price,
                protection_list_id=protection_list_id,
                protection_seq=0,
                fees={},
            )
            session.add(record)
            await session.flush()
            intent = IntentRecord(
                position_id=record.id,
                kind=IntentKind.OPEN.value,
                endpoint=intent_endpoint,
                client_id=protection_list_id,
                payload=to_jsonable(intent_payload),
                status=IntentStatus.PENDING.value,
            )
            session.add(intent)
            await session.flush()
            await session.refresh(record)
            await session.refresh(intent)
            return _to_position(record), _to_intent(intent)

    async def get_position(self, position_id: int) -> Position:
        async with self.db.session() as session:
            record = await session.get(PositionRecord, position_id)
            if record is None:
                raise PositionNotFoundError(position_id)
            return _to_position(record)

    async def find_position_by_decision(self, decision_id: str) -> Position | None:
        async with self.db.session() as session:
            record = await session.scalar(
                select(PositionRecord).where(PositionRecord.decision_id == decision_id)
            )
            return _to_position(record) if record else None

    async def active_positions(self) -> list[Position]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(PositionRecord)
                .where(PositionRecord.state.in_([s.value for s in ACTIVE_STATES]))
                .order_by(PositionRecord.id)
            )
            return [_to_position(r) for r in records]

    async def closed_positions(self, *, limit: int = 1000) -> list[Position]:
        """Closed positions with a settled result, from the most recent to the oldest."""
        async with self.db.session() as session:
            records = await session.scalars(
                select(PositionRecord)
                .where(
                    PositionRecord.state == PositionState.CLOSED.value,
                    PositionRecord.realized_pnl.is_not(None),
                )
                .order_by(PositionRecord.closed_at.desc(), PositionRecord.id.desc())
                .limit(limit)
            )
            return [_to_position(r) for r in records]

    async def realized_pnl_total(self) -> Decimal:
        async with self.db.session() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(PositionRecord.realized_pnl), 0)).where(
                    PositionRecord.state == PositionState.CLOSED.value
                )
            )
            return Decimal(total or 0)

    async def update_position(self, position_id: int, **changes: Any) -> Position:
        """Updates position fields, validating the state transition (with ``FOR UPDATE``)."""
        unknown = set(changes) - _POSITION_FIELDS
        if unknown:
            raise ValueError(f"campos desconhecidos: {sorted(unknown)}")
        async with self.db.session() as session:
            record = await session.scalar(
                select(PositionRecord).where(PositionRecord.id == position_id).with_for_update()
            )
            if record is None:
                raise PositionNotFoundError(position_id)
            now = datetime.now(UTC)
            target = changes.pop("state", None)
            if target is not None:
                target = PositionState(target)
                ensure_transition(PositionState(record.state), target)
                record.state = target.value
                if target is PositionState.PROTECTED and record.opened_at is None:
                    record.opened_at = now
                if target.is_terminal and record.closed_at is None:
                    record.closed_at = now
            for key, value in changes.items():
                stored = value
                if key == "policy":
                    stored = policy_to_json(value)
                elif key == "entry_mode":
                    stored = EntryMode(value).value
                setattr(record, key, stored)
            await session.flush()
            await session.refresh(record)
            return _to_position(record)

    # ================================================================== intents
    async def add_intent(
        self,
        position_id: int,
        kind: IntentKind,
        endpoint: str,
        client_id: str,
        payload: Mapping[str, ParamValue],
    ) -> Intent:
        async with self.db.session() as session:
            record = IntentRecord(
                position_id=position_id,
                kind=kind.value,
                endpoint=endpoint,
                client_id=client_id,
                payload=to_jsonable(payload),
                status=IntentStatus.PENDING.value,
            )
            session.add(record)
            await session.flush()
            await session.refresh(record)
            return _to_intent(record)

    async def set_intent_status(
        self, client_id: str, status: IntentStatus, error: str | None = None
    ) -> Intent:
        async with self.db.session() as session:
            record = await session.scalar(
                select(IntentRecord).where(IntentRecord.client_id == client_id).with_for_update()
            )
            if record is None:
                raise LookupError(client_id)
            record.status = status.value
            record.error = error
            await session.flush()
            await session.refresh(record)
            return _to_intent(record)

    async def get_intent(self, client_id: str) -> Intent:
        async with self.db.session() as session:
            record = await session.scalar(
                select(IntentRecord).where(IntentRecord.client_id == client_id)
            )
            if record is None:
                raise LookupError(client_id)
            return _to_intent(record)

    async def unresolved_intents(self) -> list[Intent]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(IntentRecord)
                .where(
                    IntentRecord.status.in_(
                        [IntentStatus.PENDING.value, IntentStatus.UNKNOWN.value]
                    )
                )
                .order_by(IntentRecord.id)
            )
            return [_to_intent(r) for r in records]

    async def intents_for(self, position_id: int) -> list[Intent]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(IntentRecord)
                .where(IntentRecord.position_id == position_id)
                .order_by(IntentRecord.id)
            )
            return [_to_intent(r) for r in records]

    # ================================================================== orders and fills
    async def upsert_order(self, order: Order, position_id: int | None) -> None:
        values = {
            "position_id": position_id,
            "symbol": order.symbol,
            "order_id": order.order_id,
            "order_list_id": order.order_list_id,
            "client_order_id": order.client_order_id,
            "side": order.side.value,
            "type": order.type.value,
            "status": order.status.value,
            "orig_qty": order.orig_qty,
            "executed_qty": order.executed_qty,
            "cumulative_quote_qty": order.cummulative_quote_qty,
            "price": order.price,
            "stop_price": order.stop_price,
            "trailing_delta": order.trailing_delta,
            "expiry_reason": order.expiry_reason,
        }
        statement = insert(ExchangeOrderRecord).values(**values)
        mutable: dict[str, Any] = {
            k: statement.excluded[k] for k in values if k not in {"symbol", "order_id"}
        }
        mutable["updated_at"] = func.now()
        statement = statement.on_conflict_do_update(
            constraint="uq_exchange_orders_symbol_order", set_=mutable
        )
        async with self.db.session() as session:
            await session.execute(statement)

    async def orders_for(self, position_id: int) -> list[ExchangeOrderRecord]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(ExchangeOrderRecord)
                .where(ExchangeOrderRecord.position_id == position_id)
                .order_by(ExchangeOrderRecord.order_id)
            )
            return list(records)

    async def add_fills(self, trades: Iterable[Trade], position_id: int | None) -> int:
        """Records fills idempotently; returns how many were new."""
        rows = [
            {
                "position_id": position_id,
                "symbol": t.symbol,
                "trade_id": t.id,
                "order_id": t.order_id,
                "price": t.price,
                "qty": t.qty,
                "quote_qty": t.quote_qty,
                "commission": t.commission,
                "commission_asset": t.commission_asset,
                "is_buyer": t.is_buyer,
                "is_maker": t.is_maker,
                "trade_time": t.time,
            }
            for t in trades
        ]
        if not rows:
            return 0
        statement = (
            insert(FillRecord)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_fills_symbol_trade")
            .returning(FillRecord.id)
        )
        async with self.db.session() as session:
            inserted = await session.scalars(statement)
            return len(list(inserted))

    async def fills_for(self, position_id: int) -> list[FillRecord]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(FillRecord)
                .where(FillRecord.position_id == position_id)
                .order_by(FillRecord.trade_id)
            )
            return list(records)

    # ================================================================== events and checkpoints
    async def record_event(
        self,
        kind: str,
        severity: Severity,
        payload: Mapping[str, Any] | None = None,
        *,
        position_id: int | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                EventRecord(
                    kind=kind,
                    severity=severity.value,
                    position_id=position_id,
                    payload=dict(payload or {}),
                )
            )

    async def recent_events(self, limit: int = 50) -> list[EventRecord]:
        async with self.db.session() as session:
            records = await session.scalars(
                select(EventRecord).order_by(EventRecord.id.desc()).limit(limit)
            )
            return list(records)

    async def get_checkpoint(self, key: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            record = await session.get(CheckpointRecord, key)
            return dict(record.value) if record else None

    async def set_checkpoint(self, key: str, value: Mapping[str, Any]) -> None:
        statement = insert(CheckpointRecord).values(key=key, value=dict(value))
        statement = statement.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": statement.excluded.value, "updated_at": func.now()},
        )
        async with self.db.session() as session:
            await session.execute(statement)
