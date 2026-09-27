"""Tabelas do banco (ver doc 03, §11.1). Alterações exigem uma nova migração Alembic."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Amount = Numeric(38, 18)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - API declarativa do SQLAlchemy
        Decimal: Amount,
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


class _Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PositionRecord(_Timestamps, Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    profile: Mapped[str] = mapped_column(String(12))
    decision_id: Mapped[str] = mapped_column(String(12), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    base_asset: Mapped[str] = mapped_column(String(16))
    quote_asset: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), index=True)
    entry_mode: Mapped[str] = mapped_column(String(24))
    policy: Mapped[dict[str, Any]]
    planned_qty: Mapped[Decimal]
    planned_price: Mapped[Decimal]
    protection_list_id: Mapped[str] = mapped_column(String(36))
    protection_seq: Mapped[int] = mapped_column(Integer, default=0)
    entry_qty: Mapped[Decimal | None]
    entry_quote: Mapped[Decimal | None]
    entry_price: Mapped[Decimal | None]
    protected_qty: Mapped[Decimal | None]
    exit_quote: Mapped[Decimal | None]
    exit_price: Mapped[Decimal | None]
    realized_pnl: Mapped[Decimal | None]
    exit_reason: Mapped[str | None] = mapped_column(String(24))
    fees: Mapped[dict[str, Any]] = mapped_column(default=dict)
    opened_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]


class IntentRecord(_Timestamps, Base):
    """Intenção gravada **antes** de qualquer envio à exchange."""

    __tablename__ = "intents"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    endpoint: Mapped[str] = mapped_column(String(16))
    client_id: Mapped[str] = mapped_column(String(36), unique=True)
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text)


class ExchangeOrderRecord(Base):
    """Espelho das ordens do agente na Binance."""

    __tablename__ = "exchange_orders"
    __table_args__ = (
        UniqueConstraint("symbol", "order_id", name="uq_exchange_orders_symbol_order"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    order_id: Mapped[int] = mapped_column(BigInteger)
    order_list_id: Mapped[int] = mapped_column(BigInteger)
    client_order_id: Mapped[str] = mapped_column(String(36), index=True)
    side: Mapped[str] = mapped_column(String(8))
    type: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    orig_qty: Mapped[Decimal]
    executed_qty: Mapped[Decimal]
    cumulative_quote_qty: Mapped[Decimal]
    price: Mapped[Decimal]
    stop_price: Mapped[Decimal | None]
    trailing_delta: Mapped[int | None]
    expiry_reason: Mapped[str | None] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class FillRecord(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("symbol", "trade_id", name="uq_fills_symbol_trade"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    trade_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[Decimal]
    qty: Mapped[Decimal]
    quote_qty: Mapped[Decimal]
    commission: Mapped[Decimal]
    commission_asset: Mapped[str] = mapped_column(String(16))
    is_buyer: Mapped[bool]
    is_maker: Mapped[bool]
    trade_time: Mapped[int] = mapped_column(BigInteger)


class EventRecord(Base):
    """Trilha de auditoria: alertas, mudanças de estado, comandos, anomalias."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    kind: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(8))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class CheckpointRecord(Base):
    __tablename__ = "checkpoints"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


# ============================================================================ analista (Fase 4)
class NewsItemRecord(Base):
    """Notícia coletada (conteúdo externo, não confiável) e a sua triagem."""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(32), unique=True)
    source: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(index=True)
    assets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    relevance: Mapped[float | None] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(16))
    severity: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResearchReportRecord(Base):
    """Resultado de um ciclo de pesquisa (leitura saneada, ajustes, fontes e custo)."""

    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    as_of: Mapped[datetime]
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(48))
    prompt_version: Mapped[str] = mapped_column(String(16))
    view: Mapped[dict[str, Any] | None]
    draft: Mapped[dict[str, Any] | None]
    adjustments: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sources: Mapped[list[str]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal]


class LlmUsageRecord(Base):
    """Uma chamada à API Claude: tokens, cache, buscas e custo (disjuntor de orçamento)."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(index=True)
    purpose: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(48))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer)
    web_search_requests: Mapped[int] = mapped_column(Integer)
    web_fetch_requests: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal]
