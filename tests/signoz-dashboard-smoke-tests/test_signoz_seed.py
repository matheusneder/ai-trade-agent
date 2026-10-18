"""The seed generator alone (fast, no Docker): it runs in the default ``uv run pytest``."""

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from scripts import signoz_dashboards

from tests.support.signoz_smoke import otlp, seed

END = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def day() -> seed.Telemetry:
    return seed.generate(7, 24, END)


def test_same_seed_same_data(day: seed.Telemetry) -> None:
    again = seed.generate(7, 24, END)
    assert again.summary == day.summary
    assert again.spans == day.spans
    assert again.logs == day.logs


def test_another_seed_other_data(day: seed.Telemetry) -> None:
    other = seed.generate(8, 24, END)
    assert other.spans != day.spans
    assert other.summary.last != day.summary.last


def test_everything_inside_the_window(day: seed.Telemetry) -> None:
    summary = day.summary
    assert summary.end - summary.start == 24 * 3600 * seed.SECOND
    assert all(summary.start <= s.start <= s.end <= summary.end for s in day.spans)
    assert all(summary.start <= log.time < summary.end for log in day.logs)
    for metric in day.metrics:
        assert all(summary.start <= p.time < summary.end for p in metric.points), metric.name


def test_spans_form_trees(day: seed.Telemetry) -> None:
    """Every parent exists in the same trace and contains its children."""
    by_id = {(s.trace_id, s.span_id): s for s in day.spans}
    for span in day.spans:
        if span.parent_id:
            parent = by_id[(span.trace_id, span.parent_id)]
            assert parent.start <= span.start <= span.end <= parent.end


def test_logs_point_at_their_spans(day: seed.Telemetry) -> None:
    spans = {(s.trace_id, s.span_id) for s in day.spans}
    linked = [log for log in day.logs if log.trace_id]
    assert linked
    assert all((log.trace_id, log.span_id) in spans for log in linked)


def test_counters_match_the_spans(day: seed.Telemetry) -> None:
    """The LLM cost and tokens of the metrics are the ones the spans recorded."""
    calls = [s for s in day.spans if s.service == "trade-agent.llm"]
    summary = day.summary
    assert len(calls) == summary.llm_calls
    cost = sum(float(str(s.attributes["trade_agent.cost_usd"])) for s in calls)
    assert cost == pytest.approx(summary.llm_cost)
    tokens = sum(
        int(s.attributes["gen_ai.usage.input_tokens"])
        + int(s.attributes["gen_ai.usage.output_tokens"])
        for s in calls
    )
    assert tokens == summary.llm_tokens
    metrics = {m.name: m for m in day.metrics}
    last_cost = {}
    for point in metrics["trade_agent.llm.cost"].points:  # cumulative: the last one is the total
        last_cost[tuple(sorted(point.attributes.items()))] = point.value
    assert sum(last_cost.values()) == pytest.approx(summary.llm_cost)


def test_activity_follows_the_day(day: seed.Telemetry) -> None:
    """More Binance traffic in the afternoon (UTC) than at dawn."""
    hours = [0] * 24
    for span in day.spans:
        if span.service == "trade-agent.exchange":
            hours[datetime.fromtimestamp(span.start / seed.SECOND, UTC).hour] += 1
    assert sum(hours[13:17]) > 1.2 * sum(hours[1:5])


def test_every_panel_has_something_to_show(day: seed.Telemetry) -> None:
    """Rare events happen at least once, and every metric of the dashboards is emitted."""
    summary = day.summary
    assert summary.job_failures >= 1
    assert summary.exchange_http_errors
    assert summary.entries and summary.exits
    assert len(summary.state_changes) >= 2
    assert summary.error_logs >= 1
    emitted = {m.name for m in day.metrics}
    used = {
        query["aggregations"][0]["metricName"]
        for board in signoz_dashboards.build().values()
        for panel in board["spec"]["panels"].values()
        for query in _queries(panel)
        if query.get("signal") == "metrics"
    }
    used |= {  # the ClickHouse SQL panels name their metric in the query
        name
        for board in signoz_dashboards.build().values()
        for panel in board["spec"]["panels"].values()
        for query in _queries(panel)
        for name in re.findall(r"'(trade_agent\.[\w.]+)'", query.get("query", ""))
    }
    assert "trade_agent.pnl.total" in used
    assert used <= emitted, used - emitted


def test_daily_scope_pnl_restarts_at_the_utc_midnight(day: seed.Telemetry) -> None:
    """The day's result per scope: zero at the day's first reading, the day so far after it,
    and the day before has its own result up to the last second before midnight."""
    summary = day.summary
    midnight = summary.end - summary.end % seed.DAY
    assert summary.start < midnight < summary.end  # END is 18:00: the window crosses midnight
    first = min(at for at, _ in summary.scope_pnl["global"] if at >= midnight)
    assert summary.daily_scope_pnl(first) == dict.fromkeys(seed.SCOPES, 0.0)
    assert summary.daily_scope_pnl(midnight + seed.SECOND) == {}  # no reading yet today
    before = summary.daily_scope_pnl(midnight - seed.SECOND)
    today = summary.daily_scope_pnl(summary.end)
    assert set(before) == set(today) == set(seed.SCOPES)
    assert before != today and all(value != 0 for value in today.values())
    points = dict(summary.scope_pnl["global"])
    last = max(points)
    assert today["global"] == pytest.approx(points[last] - points[first])


def test_the_otlp_requests_carry_everything(day: seed.Telemetry) -> None:
    spans = sum(
        len(scope.spans)
        for request in otlp.trace_requests(day.spans)
        for resource in request.resource_spans
        for scope in resource.scope_spans
    )
    logs = sum(
        len(scope.log_records)
        for request in otlp.log_requests(day.logs)
        for resource in request.resource_logs
        for scope in resource.scope_logs
    )
    assert (spans, logs) == (len(day.spans), len(day.logs))


def test_at_least_one_decision_cycle() -> None:
    with pytest.raises(ValueError, match="4 hours"):
        seed.generate(7, 2, END)


def _queries(panel: dict[str, Any]) -> list[dict[str, Any]]:
    (query,) = panel["spec"]["queries"]
    plugin = query["spec"]["plugin"]
    if plugin["kind"] == "signoz/CompositeQuery":
        return [q["spec"] for q in plugin["spec"]["queries"]]
    return [plugin["spec"]]
