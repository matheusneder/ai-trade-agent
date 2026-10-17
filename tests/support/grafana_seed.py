"""Synthetic, deterministic data of the agent for the Grafana dashboard smoke tests.

A small simulation of a normal day of the agent, written straight into the ORM models of
``trade_agent.persistence`` (the schema comes from the project's Alembic migrations):

* one 5-minute price path per asset (random walk with more volatility in the European and
  American hours);
* the 4h decision cycles of each profile of ``config/profiles.yaml``: entries (OPOCO with
  an entry, a take-profit and a stop order, fills and fees), exits by the decision and by
  the stop or the trailing take-profit, FOK entries that were not filled (``rejected``), a
  manual close by a Telegram command;
* the risk check: a burst of API errors pauses the global scope for 30 minutes
  (``risk.state_changed`` events and the ``states`` of the telemetry);
* a telemetry snapshot every 5 minutes, with equity, PnL and exposure computed from the
  positions and the prices (the same formulas the dashboards read);
* the analyst: news per hour (more in the busy hours), triage and research LLM calls with
  their cost, a research report per 4h round (one of them failed);
* the agent's and the other services' logs for Loki (the "Logs" dashboard).

The domain invariants hold: position states follow ``TRANSITIONS``, order IDs follow
``trade_agent.execution.ids``, the PnL and the fees come from ``summarize_fills``,
``realized_pnl`` and ``merge_fees`` on the fills themselves, and every foreign key points to
an existing position. ``Dataset.expected`` holds the aggregates the panels must show.

Reproducible: the same ``--seed``, ``--hours`` and ``--end`` give the same data.

Usage (against an EMPTY, migrated database; never the production one)::

    uv run python -m tests.support.grafana_seed --database-url postgresql+asyncpg://... \\
        --seed 42 --hours 24
"""

import argparse
import asyncio
import dataclasses
import json
import math
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trade_agent.exchange.serialization import format_decimal
from trade_agent.execution.ids import order_ids
from trade_agent.execution.orders import EntryMode
from trade_agent.execution.positions import (
    ExitReason,
    PositionState,
    ensure_transition,
    merge_fees,
    net_received_base,
    policy_to_json,
    realized_pnl,
    summarize_fills,
)
from trade_agent.persistence.models import (
    Base,
    CheckpointRecord,
    EventRecord,
    ExchangeOrderRecord,
    FillRecord,
    IntentRecord,
    LlmUsageRecord,
    NewsItemRecord,
    PositionRecord,
    ResearchReportRecord,
    TelemetrySnapshotRecord,
)
from trade_agent.research.models import AssetView, Horizon, MarketRegime, MarketView, NewsItem
from trade_agent.risk.state import GLOBAL, OpState
from trade_agent.strategy.profiles import ProfileConfig, load_strategy_config

ROOT = Path(__file__).parents[2]
STEP = timedelta(minutes=5)
CYCLE = timedelta(hours=4)
FEE_RATE = Decimal("0.001")
QUOTE = "USDT"
INITIAL_EQUITY = Decimal(1000)
ERROR_BURST_RATE = 0.27
"""API error rate of the burst: above ``api_error_rate_5m`` (0.2) of stop_conditions.yaml."""
ERROR_PAUSE = timedelta(minutes=30)
CYCLE_WORK = timedelta(minutes=3)
"""A decision cycle (research, signals, orders) ends within 3 minutes of its 4h boundary."""


@dataclass(frozen=True, slots=True)
class Asset:
    base: str
    price: Decimal
    daily_vol: float
    tick: Decimal
    step: Decimal

    @property
    def symbol(self) -> str:
        return f"{self.base}{QUOTE}"


ASSETS = (
    Asset("BTC", Decimal(67250), 0.030, Decimal("0.01"), Decimal("0.00001")),
    Asset("ETH", Decimal(3480), 0.042, Decimal("0.01"), Decimal("0.0001")),
    Asset("SOL", Decimal(182), 0.060, Decimal("0.01"), Decimal("0.001")),
    Asset("BNB", Decimal(612), 0.035, Decimal("0.01"), Decimal("0.001")),
    Asset("XRP", Decimal("0.6120"), 0.055, Decimal("0.0001"), Decimal("0.1")),
    Asset("LINK", Decimal("17.42"), 0.065, Decimal("0.001"), Decimal("0.01")),
    Asset("AVAX", Decimal("34.80"), 0.070, Decimal("0.001"), Decimal("0.01")),
    Asset("DOGE", Decimal("0.15610"), 0.075, Decimal("0.00001"), Decimal("1")),
)
NEWS_SOURCES = ("coindesk", "cointelegraph", "theblock", "decrypt", "binance")
NEWS_TOPICS = (
    ("market", "{name} sobe com fluxo comprador em exchanges"),
    ("market", "{name} recua após realização de lucros"),
    ("macro", "Dados de inflação dos EUA movem cripto e {name}"),
    ("project", "Atualização de rede do {name} entra em teste"),
    ("regulatory", "Regulador discute regras para ativos como {name}"),
    ("listing", "Binance anuncia novo par com {name}"),
    ("other", "Análise: o que esperar do {name} nesta semana"),
)
PRICING = yaml.safe_load((ROOT / "config" / "research.yaml").read_text("utf-8"))["pricing"]


# ============================================================================ the dataset
@dataclass(slots=True)
class Child:
    """A row that points to a position (``position_id`` is set once the position has an id)."""

    position: PositionRecord | None
    record: IntentRecord | ExchangeOrderRecord | FillRecord | EventRecord


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    service: str
    level: str | None
    line: str
    event: str | None = None


@dataclass(slots=True)
class Expected:
    """What the panels must show, computed here from the generated rows (not by SQL)."""

    equity: float = 0.0
    day_pnl: float = 0.0
    drawdown_pct: float = 0.0
    active_positions: int = 0
    exposure: float = 0.0
    closed_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    fees_usdt: float = 0.0
    realized_total: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    by_profile_symbol: dict[tuple[str, str], int] = field(default_factory=dict)
    decision_cycles: int = 0
    risk_state_changes: int = 0
    states: dict[str, str] = field(default_factory=dict)
    news_by_source: dict[str, int] = field(default_factory=dict)
    regime: str = ""
    exposure_multiplier: float = 0.0
    analyst_assets: list[str] = field(default_factory=list)
    reports_ok: int = 0
    reports_failed: int = 0
    llm_cost: float = 0.0
    pending_intents: int = 0
    unprotected: int = 0


@dataclass(slots=True)
class Dataset:
    seed: int
    start: datetime
    end: datetime
    positions: list[PositionRecord] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    telemetry: list[TelemetrySnapshotRecord] = field(default_factory=list)
    news: list[NewsItemRecord] = field(default_factory=list)
    reports: list[ResearchReportRecord] = field(default_factory=list)
    llm: list[LlmUsageRecord] = field(default_factory=list)
    checkpoints: list[CheckpointRecord] = field(default_factory=list)
    logs: list[LogLine] = field(default_factory=list)
    expected: Expected = field(default_factory=Expected)

    def events(self) -> list[EventRecord]:
        return [c.record for c in self.children if isinstance(c.record, EventRecord)]

    def closed(self) -> list[PositionRecord]:
        return [p for p in self.positions if p.state == "closed" and p.realized_pnl is not None]


# ============================================================================ helpers
def _q(value: Decimal, unit: Decimal) -> Decimal:
    return value.quantize(unit, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class _Fill:
    """Fill-like (``FillLike``) for the domain's summaries before the rows exist."""

    qty: Decimal
    quote_qty: Decimal
    commission: Decimal
    commission_asset: str


@dataclass(slots=True)
class _Profile:
    name: str
    config: ProfileConfig
    capital: Decimal
    state: OpState = OpState.RUNNING
    losing_streak: int = 0


@dataclass(slots=True)
class _Open:
    """An open position during the simulation."""

    record: PositionRecord
    asset: Asset
    profile: _Profile
    entry: list[_Fill]
    stop_price: Decimal
    activation: Decimal
    trailing: Decimal
    take_profit: ExchangeOrderRecord
    stop: ExchangeOrderRecord
    peak: Decimal = Decimal(0)


class _Simulation:
    def __init__(self, rng: random.Random, start: datetime, end: datetime) -> None:
        self.rng = rng
        self.start = start
        self.end = end
        self.data = Dataset(seed=0, start=start, end=end)
        strategy = load_strategy_config(ROOT / "config" / "profiles.yaml")
        self.profiles = [
            _Profile(name, p, strategy.profile_capital(name))
            for name, p in strategy.enabled_profiles().items()
        ]
        self.global_state = OpState.RUNNING
        self.global_reason: str | None = None
        self.open: list[_Open] = []
        self.order_id = rng.randrange(10**9, 2 * 10**9)
        self.list_id = rng.randrange(10**6, 2 * 10**6)
        self.trade_id = rng.randrange(10**8, 2 * 10**8)
        self.decisions: set[str] = set()
        self.realized: dict[str, Decimal] = {p.name: Decimal(0) for p in self.profiles}
        self.daily: dict[str, Decimal] = {p.name: Decimal(0) for p in self.profiles}
        self.peak = INITIAL_EQUITY
        self.day_start = INITIAL_EQUITY
        self.offset_ms = rng.randint(-40, 40)
        self.prices = self._price_paths()

    # ------------------------------------------------------------------ market
    def _steps(self) -> list[datetime]:
        count = int((self.end - self.start) / STEP)
        return [self.start + STEP * i for i in range(count + 1)]

    @staticmethod
    def _activity(at: datetime) -> float:
        """Market activity by UTC hour: quiet in Asia's night, peak in the American session."""
        hour = at.hour + at.minute / 60
        return (
            0.55
            + 0.45 * math.exp(-(((hour - 15.5) / 4.5) ** 2))
            + 0.15 * math.exp(-(((hour - 8) / 2.5) ** 2))
        )

    def _price_paths(self) -> dict[str, list[Decimal]]:
        steps = self._steps()
        per_step = 1 / math.sqrt(timedelta(days=1) / STEP)
        market = [self.rng.gauss(0, 1) for _ in steps]  # a common factor (correlated assets)
        paths: dict[str, list[Decimal]] = {}
        for asset in ASSETS:
            price = float(asset.price) * math.exp(self.rng.gauss(0, asset.daily_vol))
            drift = self.rng.gauss(0, asset.daily_vol / 3) * per_step**2
            path = []
            for at, common in zip(steps, market, strict=True):
                shock = 0.6 * common + 0.8 * self.rng.gauss(0, 1)
                price *= math.exp(drift + asset.daily_vol * per_step * self._activity(at) * shock)
                path.append(_q(Decimal(repr(price)), asset.tick))
            paths[asset.symbol] = path
        return paths

    def price(self, asset: Asset, index: int) -> Decimal:
        return self.prices[asset.symbol][index]

    # ------------------------------------------------------------------ rows
    def event(
        self,
        at: datetime,
        kind: str,
        severity: str,
        payload: dict[str, Any],
        position: PositionRecord | None = None,
    ) -> None:
        record = EventRecord(created_at=at, kind=kind, severity=severity, payload=payload)
        self.data.children.append(Child(position, record))
        level = {"info": "info", "high": "warning", "critical": "error"}[severity]
        if kind.startswith("risk."):
            level = "warning"
        self.log_agent(at, level, kind, payload)

    def log_agent(self, at: datetime, level: str, event: str, fields: dict[str, Any]) -> None:
        body = {**fields, "event": event, "level": level, "timestamp": at.isoformat()}
        if self.rng.random() < 0.6:
            body["trace_id"] = f"{self.rng.getrandbits(128):032x}"
            body["span_id"] = f"{self.rng.getrandbits(64):016x}"
        line = json.dumps(body, ensure_ascii=False, default=str)
        self.data.logs.append(LogLine(at, "agent", level, line, event))

    def _order(
        self,
        position: PositionRecord,
        at: datetime,
        *,
        client_id: str,
        side: str,
        type_: str,
        status: str,
        qty: Decimal,
        price: Decimal = Decimal(0),
        stop_price: Decimal | None = None,
        trailing: int | None = None,
        list_id: int = -1,
        executed: Decimal = Decimal(0),
        quote: Decimal = Decimal(0),
    ) -> ExchangeOrderRecord:
        self.order_id += self.rng.randint(1, 4000)
        record = ExchangeOrderRecord(
            symbol=position.symbol,
            order_id=self.order_id,
            order_list_id=list_id,
            client_order_id=client_id,
            side=side,
            type=type_,
            status=status,
            orig_qty=qty,
            executed_qty=executed,
            cumulative_quote_qty=quote,
            price=price,
            stop_price=stop_price,
            trailing_delta=trailing,
            updated_at=at,
        )
        self.data.children.append(Child(position, record))
        return record

    def _fills(
        self,
        position: PositionRecord,
        order: ExchangeOrderRecord,
        at: datetime,
        price: Decimal,
        qty: Decimal,
        *,
        buyer: bool,
        asset: Asset,
    ) -> list[_Fill]:
        """One or two fills of ``qty`` at ``price``; buys pay the fee in the base asset."""
        parts = [qty] if qty <= asset.step * 10 or self.rng.random() < 0.6 else []
        if not parts:
            first = _q(qty * Decimal(repr(self.rng.uniform(0.3, 0.7))), asset.step)
            parts = [first, qty - first]
        fills = []
        for part in parts:
            quote = _q(part * price, Decimal("0.00000001"))
            if buyer:
                fee, fee_asset = _q(part * FEE_RATE, Decimal("0.00000001")), asset.base
            else:
                fee, fee_asset = _q(quote * FEE_RATE, Decimal("0.00000001")), QUOTE
            self.trade_id += self.rng.randint(1, 50)
            self.data.children.append(
                Child(
                    position,
                    FillRecord(
                        symbol=position.symbol,
                        trade_id=self.trade_id,
                        order_id=order.order_id,
                        price=price,
                        qty=part,
                        quote_qty=quote,
                        commission=fee,
                        commission_asset=fee_asset,
                        is_buyer=buyer,
                        is_maker=False,
                        trade_time=int(at.timestamp() * 1000),
                    ),
                )
            )
            fills.append(_Fill(part, quote, fee, fee_asset))
        order.executed_qty = qty
        order.cumulative_quote_qty = sum((f.quote_qty for f in fills), Decimal(0))
        return fills

    def _transition(self, position: PositionRecord, target: PositionState) -> None:
        ensure_transition(PositionState(position.state), target)
        position.state = target.value

    # ------------------------------------------------------------------ entries
    def _decision_id(self) -> str:
        while True:
            value = f"{self.rng.getrandbits(40):010x}"
            if value not in self.decisions:
                self.decisions.add(value)
                return value

    def try_entry(self, profile: _Profile, asset: Asset, index: int, at: datetime) -> str | None:
        """Opens a position (or records a FOK that was not filled); returns the reject reason."""
        config = profile.config
        price = self.price(asset, index)
        atr_pct = Decimal(repr(asset.daily_vol * 100 / math.sqrt(6) * self.rng.uniform(1.0, 1.4)))
        policy = config.protection.policy(
            (Decimal(repr(config.protection.stop.atr_mult or 3)) * atr_pct).quantize(
                Decimal("0.01")
            )
        )
        slippage = Decimal(repr(self.rng.uniform(0, config.entry.max_slippage_bps / 4))) / 10000
        limit = _q(price * (1 + Decimal(config.entry.max_slippage_bps) / 10000), asset.tick)
        budget = profile.capital * config.allocation.max_position_pct
        budget *= Decimal(repr(self.rng.uniform(0.9, 1.0)))
        qty = _q(budget / price, asset.step)
        decision = self._decision_id()
        ids = order_ids(config.code, decision)
        position = PositionRecord(
            profile=config.code,
            decision_id=decision,
            symbol=asset.symbol,
            base_asset=asset.base,
            quote_asset=QUOTE,
            state=PositionState.PLANNED.value,
            entry_mode=EntryMode(config.entry.order).value,
            policy=policy_to_json(policy),
            planned_qty=qty,
            planned_price=limit,
            protection_list_id=ids.list_id,
            protection_seq=0,
            fees={},
            created_at=at,
            updated_at=at,
        )
        self.data.positions.append(position)
        sent = at + timedelta(milliseconds=self.rng.randint(150, 900))
        intent = IntentRecord(
            kind="open",
            endpoint="opoco",
            client_id=ids.list_id,
            payload={
                "symbol": asset.symbol,
                "listClientOrderId": ids.list_id,
                "workingType": "LIMIT",
                "workingTimeInForce": "FOK",
                "workingQuantity": format_decimal(qty),
                "workingPrice": format_decimal(limit),
            },
            status="confirmed",
            created_at=at,
            updated_at=sent,
        )
        self.data.children.append(Child(position, intent))
        self.event(at, "position.open_requested", "info", self._pos_payload(position), position)
        self.list_id += 1
        entry = self._order(
            position, sent, client_id=ids.entry_id, side="BUY", type_="LIMIT",
            status="FILLED", qty=qty, price=limit, list_id=self.list_id,
        )  # fmt: skip
        self._transition(position, PositionState.ENTRY_SENT)
        if self.rng.random() < 0.08:  # the book moved: FOK not filled, nothing to protect
            entry.status = "EXPIRED"
            self._transition(position, PositionState.REJECTED)
            position.exit_reason = ExitReason.ENTRY_REJECTED.value
            position.closed_at = position.updated_at = sent
            self.event(
                sent, "position.rejected", "high",
                self._pos_payload(position) | {"error": "FOK entry expired (no liquidity)"},
                position,
            )  # fmt: skip
            return "fok_expired"
        fill_price = _q(price * (1 + slippage), asset.tick)
        fills = self._fills(position, entry, sent, fill_price, qty, buyer=True, asset=asset)
        summary = summarize_fills(fills)
        position.entry_qty = net_received_base(summary, asset.base)
        position.entry_quote = summary.quote_qty
        position.entry_price = summary.avg_price
        position.protected_qty = _q(position.entry_qty, asset.step)
        position.fees = merge_fees(summary)
        stop_price = _q(summary.avg_price * (1 - (policy.stop_pct or Decimal(0)) / 100), asset.tick)
        activation = _q(summary.avg_price * (1 + policy.take_profit_pct / 100), asset.tick)
        bips = policy.take_profit_trailing_bips or 100
        tp = self._order(
            position, sent, client_id=ids.take_profit_id, side="SELL", type_="TAKE_PROFIT",
            status="NEW", qty=position.protected_qty, stop_price=activation, trailing=bips,
            list_id=self.list_id,
        )  # fmt: skip
        sl = self._order(
            position, sent, client_id=ids.stop_id, side="SELL", type_="STOP_LOSS",
            status="NEW", qty=position.protected_qty, stop_price=stop_price, list_id=self.list_id,
        )  # fmt: skip
        self._transition(position, PositionState.PROTECTED)
        position.opened_at = position.updated_at = sent
        self.event(sent, "position.protected", "info", self._pos_payload(position), position)
        self.open.append(
            _Open(
                position, asset, profile, fills, stop_price, activation,
                Decimal(bips) / 10000, tp, sl,
            )
        )  # fmt: skip
        return None

    @staticmethod
    def _pos_payload(position: PositionRecord) -> dict[str, Any]:
        return {
            "symbol": position.symbol,
            "state": position.state,
            "decision": position.decision_id,
        }

    # ------------------------------------------------------------------ exits
    def close(
        self, item: _Open, at: datetime, price: Decimal, reason: ExitReason
    ) -> tuple[str, str]:
        position, asset = item.record, item.asset
        ids = order_ids(item.profile.config.code, position.decision_id)
        qty = position.protected_qty or Decimal(0)
        if reason is ExitReason.STOP_LOSS:
            leg, other = item.stop, item.take_profit
        elif reason is ExitReason.TAKE_PROFIT:
            leg, other = item.take_profit, item.stop
        else:  # the agent cancels the OCO and sells at market (close intent)
            self._transition(position, PositionState.EXITING)
            self.data.children.append(
                Child(
                    position,
                    IntentRecord(
                        kind="close", endpoint="order", client_id=ids.exit_id,
                        payload={"symbol": position.symbol, "side": "SELL", "type": "MARKET",
                                 "quantity": format_decimal(qty)},
                        status="confirmed", created_at=at, updated_at=at,
                    ),
                )
            )  # fmt: skip
            self.event(
                at, "position.exit_in_progress", "info", self._pos_payload(position), position
            )
            item.take_profit.status = item.stop.status = "CANCELED"
            item.take_profit.updated_at = item.stop.updated_at = at
            leg = self._order(
                position, at, client_id=ids.exit_id, side="SELL", type_="MARKET",
                status="FILLED", qty=qty,
            )  # fmt: skip
            other = None
        leg.status = "FILLED"
        leg.updated_at = at
        if other is not None:
            other.status = "EXPIRED"
            other.updated_at = at
        exit_fills = self._fills(position, leg, at, price, qty, buyer=False, asset=asset)
        entry, exit_ = summarize_fills(item.entry), summarize_fills(exit_fills)
        self._transition(position, PositionState.CLOSED)
        position.exit_quote = exit_.quote_qty
        position.exit_price = exit_.avg_price
        position.realized_pnl = realized_pnl(entry, exit_, base_asset=asset.base, quote_asset=QUOTE)
        position.fees = merge_fees(entry, exit_)
        position.exit_reason = reason.value
        position.closed_at = position.updated_at = at
        self.event(
            at, "position.closed", "info",
            self._pos_payload(position)
            | {"reason": reason.value, "pnl": format_decimal(position.realized_pnl)},
            position,
        )  # fmt: skip
        profile = item.profile
        self.realized[profile.name] += position.realized_pnl
        self.daily[profile.name] += position.realized_pnl
        profile.losing_streak = profile.losing_streak + 1 if position.realized_pnl < 0 else 0
        self.open.remove(item)
        return position.symbol, reason.value

    def watch(self, index: int, at: datetime) -> None:
        """Exchange side: the stop or the trailing take-profit fills inside the 5 minutes."""
        for item in list(self.open):
            price = self.price(item.asset, index)
            opened = item.record.opened_at or at
            delay = timedelta(seconds=self.rng.randint(5, 280))
            when = max(opened + timedelta(seconds=30), at - delay)
            if price <= item.stop_price:
                fill = _q(item.stop_price * Decimal("0.9993"), item.asset.tick)  # slippage
                self.close(item, when, fill, ExitReason.STOP_LOSS)
                continue
            if price >= item.activation:
                item.peak = max(item.peak, price)
            if item.peak and price <= item.peak * (1 - item.trailing):
                fill = _q(item.peak * (1 - item.trailing), item.asset.tick)
                self.close(item, when, fill, ExitReason.TAKE_PROFIT)

    # ------------------------------------------------------------------ cycles
    def cycle(self, index: int, boundary: datetime, report: str) -> None:
        for order, profile in enumerate(self.profiles):
            at = boundary + timedelta(seconds=95 + 40 * order + self.rng.randint(0, 30))
            exits: list[tuple[str, str]] = []
            for item in [o for o in self.open if o.profile is profile]:
                price = self.price(item.asset, index)
                gaining = price > (item.record.entry_price or price)
                if self.rng.random() < (0.5 if gaining else 0.35):  # score below exit_score
                    price = _q(self.price(item.asset, index) * Decimal("0.9995"), item.asset.tick)
                    exits.append(self.close(item, at, price, ExitReason.DECISION))
            effective = max(self.global_state, profile.state, key=lambda s: s.rank)
            opened: list[str] = []
            rejected: list[tuple[str, str]] = []
            evaluated = self.rng.randint(9, 16)
            if effective is OpState.RUNNING:
                busy = {o.asset.base for o in self.open}  # one position per asset
                slots = profile.config.allocation.max_open_positions - sum(
                    o.profile is profile for o in self.open
                )
                candidates = [a for a in ASSETS if a.base not in busy]
                self.rng.shuffle(candidates)
                for asset in candidates[: self.rng.randint(1, 4)]:
                    if slots <= 0:
                        rejected.append((asset.symbol, "max_open_positions"))
                        continue
                    if self.rng.random() < 0.3:
                        rejected.append(
                            (asset.symbol, self.rng.choice(["llm_veto", "reward_risk", "score"]))
                        )
                        continue
                    reason = self.try_entry(profile, asset, index, at)
                    if reason:
                        rejected.append((asset.symbol, reason))
                    else:
                        opened.append(asset.symbol)
                        slots -= 1
            payload = {
                "profile": profile.name,
                "state": effective.value,
                "evaluated": evaluated,
                "dry_run": False,
                "research": report,
                "opened": opened,
                "exits": [list(e) for e in exits],
                "rejected": [list(r) for r in rejected],
            }
            self.event(at + timedelta(seconds=2), "decision.cycle", "info", payload)

    def set_global(self, at: datetime, state: OpState, reason: str | None, source: str) -> None:
        self.global_state, self.global_reason = state, reason
        until = (at + ERROR_PAUSE).isoformat() if state is OpState.PAUSED else None
        payload = {
            "scope": GLOBAL, "source": source, "state": state.value, "reason": reason,
            "since": at.isoformat(), "until": until, "flattened": False,
        }  # fmt: skip
        severity = "info" if state is OpState.RUNNING else "critical"
        self.event(at, "risk.state_changed", severity, payload)

    # ------------------------------------------------------------------ telemetry
    def snapshot(self, index: int, at: datetime, error_rate: float, weight: int) -> None:
        unrealized: dict[str, Decimal] = {p.name: Decimal(0) for p in self.profiles}
        exposure = Decimal(0)
        for item in self.open:
            value = (item.record.protected_qty or 0) * self.price(item.asset, index)
            exposure += value
            unrealized[item.profile.name] += value - (item.record.entry_quote or 0)
        realized_total = sum(self.realized.values(), Decimal(0))
        unrealized_total = sum(unrealized.values(), Decimal(0))
        equity = (INITIAL_EQUITY + realized_total + unrealized_total).quantize(Decimal("0.0001"))
        if at.hour == 0 and at.minute < 5:  # UTC day turn: the day's PnL restarts
            self.day_start = equity
            self.daily = dict.fromkeys(self.daily, Decimal(0))
        self.peak = max(self.peak, equity)
        self.offset_ms += self.rng.randint(-3, 3)
        btc = self.prices["BTCUSDT"]
        back = max(index - 12, 0)
        states = {GLOBAL: self.global_state.value} | {p.name: p.state.value for p in self.profiles}
        self.data.telemetry.append(
            TelemetrySnapshotRecord(
                at=at,
                equity=equity,
                realized_pnl=realized_total,
                unrealized_pnl=unrealized_total.quantize(Decimal("0.00000001")),
                day_start_equity=self.day_start,
                peak_equity=self.peak,
                drawdown_pct=float((self.peak - equity) / self.peak * 100),
                exposure=exposure.quantize(Decimal("0.00000001")),
                active_positions=len(self.open),
                api_error_rate=error_rate,
                used_weight_1m=weight,
                clock_offset_ms=self.offset_ms,
                btc_change_1h=float((btc[index] - btc[back]) / btc[back] * 100),
                quote_deviation=round(self.rng.uniform(-0.0006, 0.0006), 6),
                fear_greed=52 + int(10 * math.sin(index / 70)),
                states=states,
                profiles={
                    p.name: {
                        "daily_pnl": format_decimal(self.daily[p.name]),
                        "losing_streak": p.losing_streak,
                        "pnl": format_decimal(self.realized[p.name] + unrealized[p.name]),
                    }
                    for p in self.profiles
                },
            )
        )

    # ------------------------------------------------------------------ analyst
    def _llm(self, at: datetime, purpose: str, model: str, scale: float) -> LlmUsageRecord:
        price = PRICING["models"][model]
        inp = int(self.rng.uniform(6000, 14000) * scale)
        out = int(self.rng.uniform(900, 2500) * scale)
        write = int(self.rng.uniform(0, 3000))
        read = int(self.rng.uniform(2000, 9000))
        searches = self.rng.randint(1, 5) if purpose == "research" else 0
        fetches = self.rng.randint(0, 3) if purpose == "research" else 0
        cost = (
            inp * price["input"]
            + out * price["output"]
            + write * price["input"] * PRICING["cache_write_multiplier"]
            + read * price["input"] * PRICING["cache_read_multiplier"]
        ) / 1e6 + searches * PRICING["web_search_per_1000"] / 1000
        record = LlmUsageRecord(
            at=at, purpose=purpose, model=model, input_tokens=inp, output_tokens=out,
            cache_creation_input_tokens=write, cache_read_input_tokens=read,
            web_search_requests=searches, web_fetch_requests=fetches,
            cost_usd=Decimal(repr(round(cost, 6))),
        )  # fmt: skip
        self.data.llm.append(record)
        self.log_agent(at, "info", "llm.usage", {"purpose": purpose, "model": model,
                                                  "cost_usd": str(record.cost_usd)})  # fmt: skip
        return record

    def research(self, boundary: datetime, failed: bool) -> str:
        at = boundary + timedelta(seconds=self.rng.randint(20, 80))
        calls = [self._llm(at, "research", "claude-opus-5", 1.6)]
        if failed:
            self.data.reports.append(
                ResearchReportRecord(
                    created_at=at + timedelta(seconds=40), as_of=boundary, trigger="ciclo:"
                    + "+".join(sorted(p.name for p in self.profiles)), status="failed",
                    model="claude-opus-5", prompt_version="v3", view=None, draft=None,
                    error="timeout após 300 s", cost_usd=calls[0].cost_usd,
                )
            )  # fmt: skip
            return "falhou: timeout após 300 s"
        calls.append(self._llm(at + timedelta(seconds=30), "analyst", "claude-opus-5", 1.0))
        regime = self.rng.choice(list(MarketRegime))
        picks = self.rng.sample(ASSETS, 5)
        view = MarketView(
            as_of=boundary,
            market_regime=regime,
            global_sentiment=round(self.rng.uniform(-0.4, 0.6), 2),
            exposure_multiplier={"risk_on": 1.0, "neutral": 0.8, "risk_off": 0.5}[regime],
            global_risk_flags=("macro_week",) if regime is MarketRegime.RISK_OFF else (),
            assets=tuple(
                AssetView(
                    asset=a.base,
                    sentiment=round(self.rng.uniform(-0.6, 0.7), 2),
                    confidence=round(self.rng.uniform(0.4, 0.9), 2),
                    horizon=self.rng.choice(list(Horizon)),
                    veto=self.rng.random() < 0.1,
                    rationale=f"Leitura sintética de {a.base}: fluxo e notícias do período.",
                    sources=("coindesk", "binance"),
                )
                for a in picks
            ),
        )
        self.data.reports.append(
            ResearchReportRecord(
                created_at=at + timedelta(seconds=75), as_of=boundary,
                trigger="ciclo:" + "+".join(sorted(p.name for p in self.profiles)),
                status="ok", model="claude-opus-5", prompt_version="v3",
                view=view.model_dump(mode="json"), draft=None, adjustments=[],
                sources=["coindesk", "binance"],
                cost_usd=sum((c.cost_usd for c in calls), Decimal(0)),
            )
        )  # fmt: skip
        return "ok"

    def news_hour(self, hour: datetime) -> None:
        count = max(0, round(self.rng.gauss(14 * self._activity(hour), 3)))
        for _ in range(count):
            at = hour + timedelta(seconds=self.rng.randint(0, 3599))
            if not self.start <= at <= self.end:
                continue
            asset = self.rng.choice(ASSETS)
            category, template = self.rng.choice(NEWS_TOPICS)
            source = self.rng.choices(NEWS_SOURCES, weights=(5, 4, 2, 2, 1))[0]
            title = template.format(name=asset.base) + f" ({at:%H:%M})"
            self._news(at, source, title, category, asset.base)
        if count and self.start <= hour + timedelta(minutes=50) <= self.end:
            self._llm(hour + timedelta(minutes=50), "triage", "claude-sonnet-5", 0.6)

    def _news(self, at: datetime, source: str, title: str, category: str, asset: str) -> None:
        url = f"https://example.invalid/{source}/{at:%Y%m%d%H%M%S}-{self.rng.getrandbits(24):06x}"
        item = NewsItem(source=source, title=title, published_at=at, url=url, assets=(asset,))
        severity = self.rng.choices(("low", "medium", "high"), weights=(6, 3, 1))[0]
        if category == "hack":
            severity = "critical"
        self.data.news.append(
            NewsItemRecord(
                dedupe_key=item.dedupe_key, source=source, title=title, url=url,
                summary="Resumo sintético para os testes de fumaça.", published_at=at,
                assets=[asset], relevance=round(self.rng.uniform(0.1, 0.95), 2),
                category=category, severity=severity,
                created_at=min(at + timedelta(minutes=3), self.end),
            )
        )  # fmt: skip

    # ------------------------------------------------------------------ the day
    def _plan(self, steps: Sequence[datetime]) -> "_Plan":
        """When the cycles, the API error burst and the manual close happen."""
        boundary = self.start.replace(hour=0, minute=0, second=0, microsecond=0) + CYCLE
        while boundary <= self.start:
            boundary += CYCLE
        cycles = []
        while boundary < self.end - timedelta(minutes=5):
            cycles.append(boundary)
            boundary += CYCLE
        # the API error burst: between two cycles, well inside the window
        target = self.start + (self.end - self.start) * self.rng.uniform(0.35, 0.6)
        burst = min(steps, key=lambda s: abs(s - target))
        while any(abs(burst - c) < timedelta(hours=1) for c in cycles):
            burst += timedelta(minutes=30)
        return _Plan(
            cycles=cycles,
            failed_round=cycles[len(cycles) // 2] if len(cycles) > 2 else None,
            burst=burst,
            manual=self.start + (self.end - self.start) * self.rng.uniform(0.65, 0.8),
        )

    def _agent_started(self) -> None:
        started = self.start + timedelta(seconds=7)
        payload = {"clock_offset_ms": self.offset_ms, "reconcile": {"positions": 0}}
        self.event(started, "agent.started", "info", payload)
        self.data.checkpoints.append(
            CheckpointRecord(
                key="agent.instance", value={"started_at": started.isoformat()}, updated_at=started
            )
        )

    def _risk(self, at: datetime, plan: "_Plan") -> float:
        """The API error rate of the step; the burst pauses the global scope for 30 minutes."""
        error_rate = round(abs(self.rng.gauss(0.002, 0.004)), 4)
        if plan.burst <= at < plan.burst + timedelta(minutes=10):
            error_rate = ERROR_BURST_RATE + round(self.rng.uniform(0, 0.05), 4)
        if at == plan.burst:
            reason = f"api_error_rate_5m {error_rate:.0%} ≥ 20%"
            self.set_global(at + timedelta(seconds=20), OpState.PAUSED, reason, "monitor")
            failure = {"job": "check_risk", "error": "HTTPStatusError('503 Service Unavailable')"}
            self.event(at + timedelta(seconds=21), "runtime.task_failed", "high", failure)
        resume = plan.burst + ERROR_PAUSE
        if self.global_state is OpState.PAUSED and at >= resume:
            self.set_global(resume + timedelta(seconds=3), OpState.RUNNING, None, "cooldown")
        return error_rate

    def _manual_close(self, index: int, at: datetime) -> None:
        """``/close`` on Telegram: the oldest open position is sold at market."""
        item = min(self.open, key=lambda o: o.record.opened_at or at)
        self.event(at, "telegram.command", "info", {"text": f"/close {item.record.symbol}"})
        price = _q(self.price(item.asset, index) * Decimal("0.9996"), item.asset.tick)
        self.close(item, at + timedelta(seconds=2), price, ExitReason.MANUAL)

    def run(self) -> Dataset:
        steps = self._steps()
        plan = self._plan(steps)
        news_hours = iter(sorted({s.replace(minute=0) for s in steps}))
        next_news: datetime | None = next(news_hours)
        self._agent_started()
        self._service_logs(steps)
        hack = self.start + (self.end - self.start) * self.rng.uniform(0.3, 0.7)
        title = "Ponte cross-chain sofre ataque; tokens afetados incluem SOL"
        self._news(hack, "theblock", title, "hack", "SOL")
        for index, at in enumerate(steps):
            while next_news is not None and next_news <= at:
                self.news_hour(next_news)
                next_news = next(news_hours, None)
            previous = steps[index - 1] if index else at
            if index:
                self.watch(index, at)
            for boundary in plan.cycles:
                if previous < boundary + CYCLE_WORK <= at:
                    report = self.research(boundary, failed=boundary == plan.failed_round)
                    self.cycle(index, boundary, report)
            error_rate = self._risk(at, plan)
            if self.open and previous < plan.manual <= at - timedelta(seconds=2):
                self._manual_close(index, plan.manual)
            weight = self.rng.randint(8, 60)
            if any(timedelta(0) <= at - c < timedelta(minutes=10) for c in plan.cycles):
                weight += self.rng.randint(150, 420)  # candles and book of the universe
            self.snapshot(index, at, error_rate, weight)
            if index % 12 == 0:
                equity = str(self.data.telemetry[-1].equity)
                self.log_agent(at, "debug", "telemetry.recorded", {"equity": equity})
        self._tracebacks(steps)
        return self.data

    # ------------------------------------------------------------------ other services' logs
    def _service_logs(self, steps: Sequence[datetime]) -> None:
        def add(at: datetime, service: str, level: str, line: str) -> None:
            self.data.logs.append(LogLine(at, service, level, line))

        for at in steps[::2]:
            ts = at.strftime("%Y-%m-%dT%H:%M:%SZ")
            add(at, "grafana", "info", f'logger=context t={ts} level=info msg="Request Completed"')
            add(at, "loki", "info", f'level=info ts={ts} caller=table_manager.go msg="uploading"')
            if self.rng.random() < 0.5:
                add(at, "alloy", "info", f'ts={ts} level=info msg="tailer running"')
        for at in steps[6::12]:
            pg = at.strftime("%Y-%m-%d %H:%M:%S.000 UTC")
            add(at, "postgres", "info", f"{pg} [64] LOG:  checkpoint complete: wrote 42 buffers")
        window = steps[len(steps) // 8 : -len(steps) // 8]  # far from the window's edges
        for at in self.rng.sample(window, 6):
            ts = at.strftime("%Y-%m-%dT%H:%M:%SZ")
            add(at, "grafana", "warn", f'logger=ngalert t={ts} level=warn msg="slow rule eval"')
        for at in self.rng.sample(window, 2):
            pg = at.strftime("%Y-%m-%d %H:%M:%S.000 UTC")
            add(at, "postgres", "error", f"{pg} [912] FATAL:  terminating idle-session connection")
            add(at, "postgres", "warning", f"{pg} [913] WARNING:  there is no transaction")
        recent = steps[-len(steps) // 6 : -len(steps) // 24]  # in the Logs dashboard's 6 h
        for at in [*self.rng.sample(window, 2), self.rng.choice(recent)]:
            body = {"level": "warn", "ts": at.isoformat(), "msg": "slow exporter batch"}
            add(at, "jaeger", "warn", json.dumps(body))
        at = self.rng.choice(recent)
        pg = at.strftime("%Y-%m-%d %H:%M:%S.000 UTC")
        add(at, "postgres", "error", f"{pg} [977] ERROR:  canceling statement due to timeout")

    def _tracebacks(self, steps: Sequence[datetime]) -> None:
        window = steps[len(steps) // 8 : -len(steps) // 8]
        for at in self.rng.sample(window, 2):
            line = (
                "Traceback (most recent call last):\n"
                '  File "/app/src/trade_agent/runtime.py", line 142, in run\n'
                "    await self.store.record_event(kind, severity, payload)\n"
                "ConnectionResetError: [Errno 104] Connection reset by peer"
            )
            self.data.logs.append(LogLine(at, "agent", "error", line))


@dataclass(frozen=True, slots=True)
class _Plan:
    cycles: list[datetime]
    failed_round: datetime | None
    burst: datetime
    manual: datetime


# ============================================================================ expected values
def _expected(data: Dataset) -> Expected:
    last = data.telemetry[-1]
    closed = data.closed()
    pnl = [p.realized_pnl or Decimal(0) for p in closed]
    gains = sum((v for v in pnl if v > 0), Decimal(0))
    losses = -sum((v for v in pnl if v < 0), Decimal(0))
    reasons: dict[str, int] = {}
    by_key: dict[tuple[str, str], int] = {}
    for p in closed:
        reasons[p.exit_reason or ""] = reasons.get(p.exit_reason or "", 0) + 1
        by_key[(p.profile, p.symbol)] = by_key.get((p.profile, p.symbol), 0) + 1
    events = data.events()
    news: dict[str, int] = {}
    for item in data.news:
        news[item.source] = news.get(item.source, 0) + 1
    latest_ok = max((r for r in data.reports if r.status == "ok"), key=lambda r: r.created_at)
    view = latest_ok.view or {}
    return Expected(
        equity=float(last.equity),
        day_pnl=float(last.equity - last.day_start_equity),
        drawdown_pct=last.drawdown_pct,
        active_positions=last.active_positions,
        exposure=float(last.exposure),
        closed_trades=len(closed),
        win_rate_pct=100 * sum(v > 0 for v in pnl) / len(pnl),
        profit_factor=float(gains / losses),
        fees_usdt=float(sum((Decimal(p.fees.get(QUOTE, "0")) for p in closed), Decimal(0))),
        realized_total=float(sum(pnl, Decimal(0))),
        exit_reasons=reasons,
        by_profile_symbol=by_key,
        decision_cycles=sum(e.kind == "decision.cycle" for e in events),
        risk_state_changes=sum(e.kind == "risk.state_changed" for e in events),
        states=dict(last.states),
        news_by_source=news,
        regime=view["market_regime"],
        exposure_multiplier=view["exposure_multiplier"],
        analyst_assets=[a["asset"] for a in view["assets"]],
        reports_ok=sum(r.status == "ok" for r in data.reports),
        reports_failed=sum(r.status == "failed" for r in data.reports),
        llm_cost=float(sum((u.cost_usd for u in data.llm), Decimal(0))),
        pending_intents=0,
        unprotected=sum(p.state == "unprotected" for p in data.positions),
    )


def _timestamps(data: Dataset) -> Iterator[datetime]:
    yield from (_child_time(c.record) for c in data.children)
    yield from (p.updated_at for p in data.positions)
    yield from (t.at for t in data.telemetry)
    yield from (n.created_at for n in data.news)
    yield from (r.created_at for r in data.reports)
    yield from (u.at for u in data.llm)
    yield from (log.at for log in data.logs)


def _complete(data: Dataset) -> bool:
    """Whether every panel and table gets data (wins and losses, an open position...)."""
    closed = data.closed()
    return (
        any((p.realized_pnl or 0) > 0 for p in closed)
        and any((p.realized_pnl or 0) < 0 for p in closed)
        and len({p.exit_reason for p in closed}) >= 3
        and any(p.state == "rejected" for p in data.positions)
        and data.telemetry[-1].active_positions > 0
        and any(r.status == "ok" for r in data.reports)
        and any(r.status == "failed" for r in data.reports)
        and data.telemetry[-1].drawdown_pct < 10  # the "Drawdown elevado" alert stays normal
        and any(e.kind == "risk.state_changed" for e in data.events())
    )


def generate(seed: int = 42, hours: int = 24, end: datetime | None = None) -> Dataset:
    """The synthetic day ending at ``end`` (default: now). Deterministic for the same inputs.

    Some seeds give a day without, say, a losing trade; the generator then tries the next
    sub-seed (``seed:1``, ``seed:2``...), so the result still depends only on the inputs.
    """
    if hours < 12:
        raise ValueError("at least 12 hours (three decision cycles)")
    end = (end or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    for attempt in range(200):
        rng = random.Random(f"{seed}:{attempt}")  # noqa: S311 - synthetic data, not security
        data = _Simulation(rng, start, end).run()
        outside = [at for at in _timestamps(data) if not start <= at <= end]
        if outside:
            raise AssertionError(f"rows outside the window {start}..{end}: {outside[:5]}")
        if _complete(data):
            data.seed = seed
            data.children.sort(key=lambda c: _child_time(c.record))
            data.expected = _expected(data)
            return data
    raise RuntimeError(f"no complete day for seed {seed} in 200 attempts")


def _child_time(record: Any) -> datetime:
    if isinstance(record, EventRecord):
        return record.created_at
    if isinstance(record, IntentRecord):
        return record.created_at
    if isinstance(record, FillRecord):
        return datetime.fromtimestamp(record.trade_time / 1000, UTC)
    assert isinstance(record, ExchangeOrderRecord)
    return record.updated_at


# ============================================================================ persistence
async def insert(url: str, data: Dataset) -> None:
    """Writes the dataset into an empty database, in time order (ids grow with time)."""
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            await _insert(session, data)
    finally:
        await engine.dispose()


async def _insert(session: AsyncSession, data: Dataset) -> None:
    def add(rows: Iterable[Base]) -> None:
        session.add_all(list(rows))

    positions = sorted(data.positions, key=lambda position: position.created_at)
    news = sorted(data.news, key=lambda item: item.published_at)
    reports = sorted(data.reports, key=lambda report: report.created_at)
    add(positions)
    await session.flush()
    for child in data.children:
        if child.position is not None:
            child.record.position_id = child.position.id
    for kind in (IntentRecord, ExchangeOrderRecord, FillRecord, EventRecord):
        add(c.record for c in data.children if isinstance(c.record, kind))
        await session.flush()
    add(data.telemetry)
    add(news)
    add(reports)
    add(data.llm)
    add(data.checkpoints)


def loki_streams(data: Dataset) -> list[dict[str, Any]]:
    """The logs as Loki push streams, with the labels and the metadata Alloy would set."""
    streams: dict[tuple[str, str | None], list[list[Any]]] = {}
    for log in sorted(data.logs, key=lambda item: item.at):
        value: list[Any] = [str(int(log.at.timestamp() * 1e9)), log.line]
        if log.event:  # structured metadata: not part of the stream's identity
            value.append({"event": log.event})
        streams.setdefault((log.service, log.level), []).append(value)
    result = []
    for (service, level), values in streams.items():
        labels = {"service": service} | ({"level": level} if level else {})
        result.append({"stream": labels, "values": values})
    return result


def log_counts(data: Dataset, since: datetime) -> dict[str, int]:
    """The "Logs" stat panels: errors, warnings and agent tracebacks since ``since``."""
    recent = [log for log in data.logs if log.at > since]
    return {
        "errors": sum(log.level in {"error", "critical", "fatal"} for log in recent),
        "warnings": sum(log.level in {"warning", "warn"} for log in recent),
        "tracebacks": sum(
            log.service == "agent" and log.line.startswith("Traceback") for log in recent
        ),
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--database-url", required=True, help="postgresql+asyncpg://... (empty, migrated)"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--end", type=datetime.fromisoformat, default=None,
                        help="end of the window (ISO 8601, UTC); default: now")  # fmt: skip
    args = parser.parse_args(argv)
    data = generate(args.seed, args.hours, args.end)
    asyncio.run(insert(args.database_url, data))
    summary = dataclasses.asdict(data.expected)
    summary.pop("by_profile_symbol")
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
