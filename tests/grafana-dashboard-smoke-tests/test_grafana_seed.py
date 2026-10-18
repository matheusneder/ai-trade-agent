"""The synthetic day keeps the agent's invariants (no Docker, no browser: always runs)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support.grafana_seed import (
    STEP,
    Dataset,
    daily_scope_pnl,
    generate,
    log_counts,
    loki_streams,
)
from trade_agent.execution.positions import (
    PositionState,
    realized_pnl,
    summarize_fills,
)
from trade_agent.persistence.models import (
    EventRecord,
    ExchangeOrderRecord,
    FillRecord,
    IntentRecord,
)

END = datetime(2026, 10, 16, 15, 7, 31, tzinfo=UTC)


@pytest.fixture(scope="module")
def day() -> Dataset:
    return generate(seed=42, hours=24, end=END)


def test_same_inputs_give_the_same_day(day: Dataset) -> None:
    again = generate(seed=42, hours=24, end=END)
    assert dataclasses.asdict(again.expected) == dataclasses.asdict(day.expected)
    assert [p.decision_id for p in again.positions] == [p.decision_id for p in day.positions]
    other = generate(seed=7, hours=24, end=END)
    assert [p.decision_id for p in other.positions] != [p.decision_id for p in day.positions]


@pytest.mark.parametrize(("seed", "hours"), [(1, 12), (2, 24), (3, 36), (4, 48)])
def test_every_seed_and_window_fills_every_panel(seed: int, hours: int) -> None:
    data = generate(seed=seed, hours=hours, end=END)
    assert data.end - data.start == timedelta(hours=hours)
    assert len(data.telemetry) == hours * 12 + 1  # a snapshot every 5 minutes
    assert data.expected.closed_trades > 0 and data.expected.active_positions > 0
    assert len(data.expected.exit_reasons) >= 3
    assert data.expected.reports_ok and data.expected.reports_failed


def test_rows_point_to_existing_positions_and_valid_states(day: Dataset) -> None:
    ids = {id(p) for p in day.positions}
    assert all(c.position is None or id(c.position) in ids for c in day.children)
    for position in day.positions:
        state = PositionState(position.state)
        assert state.is_terminal or state is PositionState.PROTECTED
        assert position.created_at <= (position.opened_at or position.created_at)
        if state.is_terminal:
            assert position.closed_at is not None and position.exit_reason
    decisions = [p.decision_id for p in day.positions]
    assert len(decisions) == len(set(decisions))
    orders = [c.record for c in day.children if isinstance(c.record, ExchangeOrderRecord)]
    assert len({(o.symbol, o.order_id) for o in orders}) == len(orders)
    fills = [c.record for c in day.children if isinstance(c.record, FillRecord)]
    assert len({(f.symbol, f.trade_id) for f in fills}) == len(fills)
    intents = [c.record for c in day.children if isinstance(c.record, IntentRecord)]
    assert len({i.client_id for i in intents}) == len(intents)
    assert all(i.status == "confirmed" for i in intents)


def test_pnl_and_fees_come_from_the_fills(day: Dataset) -> None:
    for position in day.closed():
        fills = [
            c.record
            for c in day.children
            if c.position is position and isinstance(c.record, FillRecord)
        ]
        entry = summarize_fills(f for f in fills if f.is_buyer)
        exit_ = summarize_fills(f for f in fills if not f.is_buyer)
        assert position.entry_quote == entry.quote_qty
        assert position.exit_quote == exit_.quote_qty
        assert position.realized_pnl == realized_pnl(
            entry, exit_, base_asset=position.base_asset, quote_asset="USDT"
        )
        assert Decimal(position.fees["USDT"]) == exit_.fees["USDT"]
        assert position.protected_qty is not None and position.entry_qty is not None
        assert exit_.base_qty == position.protected_qty <= position.entry_qty


def test_equity_follows_the_trades(day: Dataset) -> None:
    closed_pnl = sum((p.realized_pnl or Decimal(0) for p in day.closed()), Decimal(0))
    last = day.telemetry[-1]
    assert last.realized_pnl == closed_pnl
    assert last.equity == (Decimal(1000) + last.realized_pnl + last.unrealized_pnl).quantize(
        Decimal("0.0001")
    )
    assert last.active_positions == sum(p.state == "protected" for p in day.positions)
    assert all(b.at - a.at == STEP for a, b in zip(day.telemetry, day.telemetry[1:], strict=False))
    assert all(t.peak_equity >= t.equity for t in day.telemetry)
    assert day.expected.realized_total == pytest.approx(float(closed_pnl))


def test_daily_scope_pnl_restarts_at_the_utc_midnight(day: Dataset) -> None:
    """Zero at the day's first snapshot, the day so far after it; the day before keeps its own
    result up to the last second before midnight."""
    midnight = END.replace(hour=0, minute=0, second=0)
    first = next(t.at for t in day.telemetry if t.at >= midnight)
    scopes = {"global", *day.telemetry[-1].profiles}
    assert daily_scope_pnl(day, first) == dict.fromkeys(scopes, 0.0)
    assert daily_scope_pnl(day, midnight) == {}  # no snapshot yet today
    before = daily_scope_pnl(day, midnight - timedelta(seconds=1))
    today = daily_scope_pnl(day, END)
    assert set(before) == set(today) == scopes
    assert before != today and today["global"] != 0
    last = day.telemetry[-1]
    start = next(t for t in day.telemetry if t.at == first)
    expected = last.realized_pnl + last.unrealized_pnl - start.realized_pnl - start.unrealized_pnl
    assert today["global"] == float(expected)


def test_events_and_logs_tell_the_same_story(day: Dataset) -> None:
    events = [c.record for c in day.children if isinstance(c.record, EventRecord)]
    states = [e.payload["state"] for e in events if e.kind == "risk.state_changed"]
    assert states == ["paused", "running"]
    assert sum(e.kind == "decision.cycle" for e in events) == day.expected.decision_cycles
    logged = [log.event for log in day.logs if log.event]
    assert sorted(logged.count(k) for k in {e.kind for e in events}) == sorted(
        sum(e.kind == k for e in events) for k in {e.kind for e in events}
    )
    counts = log_counts(day, day.end - timedelta(hours=24))
    assert counts["errors"] > 0 and counts["warnings"] > 0 and counts["tracebacks"] == 2
    labels = [s["stream"] for s in loki_streams(day)]
    assert len(labels) == len({tuple(sorted(s.items())) for s in labels})
