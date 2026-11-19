"""The tracker of a profile's lead traders (D-052): who it follows, what a cycle reads, and
every failure of the source ending in fewer entries, never in a wrong one."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from tests.support.leads import NOW, FakeSource, active, stats, trade
from trade_agent.leads import tracker as tracker_module
from trade_agent.leads.consensus import BUY, NO_DATA, NO_LEADS, OK, STALE_RANKING
from trade_agent.leads.ranking import IDLE, LOW_ROI, WEAK_RECENT
from trade_agent.leads.source import LeadSourceError, LeadSourceRefusedError, LeadTrade
from trade_agent.leads.tracker import (
    CHECKPOINT,
    RECOVERED_EVENT,
    SELECTED_EVENT,
    UNAVAILABLE_EVENT,
    UNREADABLE,
    Followed,
    LeadTracker,
)
from trade_agent.persistence.store import Severity, Store
from trade_agent.strategy.profiles import LeadsConfig

D = Decimal
PROFILE = "lead_consensus_1h"
KEY = CHECKPOINT.format(profile=PROFILE)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _store(checkpoint: dict[str, Any] | None = None) -> AsyncMock:
    store = AsyncMock(spec=Store)
    store.get_checkpoint.return_value = checkpoint
    return store


def _buys(symbol: str = "SOLUSDT", **kw: Any) -> list[LeadTrade]:
    """An active trader that also bought 10% of its balance of ``symbol`` an hour ago."""
    return active(trade(symbol, quote="200", **kw))


def _source(**trades: list[LeadTrade]) -> FakeSource:
    """Portfolios ``a``, ``b``... that pass every figure, each with its trades."""
    return FakeSource([stats(lead) for lead in trades], trades=trades)


class Harness:
    def __init__(
        self,
        source: FakeSource,
        *,
        store: AsyncMock | None = None,
        config: LeadsConfig | None = None,
        notify: bool = True,
    ) -> None:
        self.source, self.store, self.clock = source, store or _store(), Clock()
        self.alerts: list[tuple[Severity, str]] = []
        self.waits: list[float] = []

        async def notify_(severity: Severity, text: str) -> None:
            self.alerts.append((severity, text))

        async def sleep(seconds: float) -> None:
            self.waits.append(seconds)

        self.tracker = LeadTracker(
            profile=PROFILE,
            config=config or LeadsConfig(),
            source=source,  # type: ignore[arg-type]
            store=self.store,
            quote_asset="USDT",
            notify=notify_ if notify else None,
            clock=self.clock,
            sleep=sleep,
        )

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            call.args[2] for call in self.store.record_event.await_args_list if call.args[0] == kind
        ]


async def test_the_first_reading_ranks_follows_and_announces() -> None:
    idle = [trade(hours_ago=24 * 30)]
    source = _source(a=_buys(), b=_buys(), c=idle)
    source.pool.append(stats("weak", roi_pct=1.0))
    h = Harness(source)
    assert h.tracker.view().status == STALE_RANKING  # before any reading: no entry
    await h.tracker.refresh()

    view = h.tracker.view()
    assert (view.status, view.selected, view.fresh, view.as_of) == (OK, 2, 2, NOW)
    assert view.backings["SOLUSDT"].buyers == ("a", "b")
    assert view.backings["SOLUSDT"].consensus(2) == BUY
    # one ranking page, then the curve and the trades of each candidate: nothing read twice
    assert source.calls == [
        "ranking:1", "curve:a", "trades:a", "curve:b", "trades:b", "curve:c", "trades:c",
    ]  # fmt: skip
    assert h.waits == [0.5] * 7  # every request spaced from the next one
    (selected,) = h.events(SELECTED_EVENT)
    assert (selected["profile"], selected["pool"], selected["examined"]) == (PROFILE, 4, 3)
    assert [lead["id"] for lead in selected["selected"]] == ["a", "b"]
    assert selected["rejected"] == {LOW_ROI: 1, IDLE: 1}
    first = selected["selected"][0]
    assert (first["nickname"], first["balance"], first["roi_pct"]) == ("lead-a", "2000", 12.0)
    assert first["positive_weeks"] == 1.0 and first["score"] > 0
    h.store.set_checkpoint.assert_awaited_once_with(
        KEY, {"ranked_at": NOW.isoformat(), "leads": selected["selected"]}
    )
    assert h.alerts == [
        (Severity.INFO, f"{PROFILE}: traders acompanhados: lead-a (0.58), lead-b (0.58)")
    ]


async def test_between_rankings_only_the_followed_traders_trades_are_read() -> None:
    source = _source(a=_buys(), b=active())
    h = Harness(source)
    await h.tracker.refresh()
    assert "SOLUSDT" in h.tracker.view().backings
    source.calls.clear()
    source.history["a"] = active()  # sold nothing, but the purchase left the last 100 trades
    h.clock.advance(minutes=15)
    await h.tracker.refresh()
    assert source.calls == ["trades:a", "trades:b"]
    view = h.tracker.view()
    assert (view.status, view.as_of, dict(view.backings)) == (OK, h.clock.now, {})
    assert len(h.events(SELECTED_EVENT)) == 1 and len(h.alerts) == 1


async def test_a_new_ranking_replaces_the_followed_traders_and_says_who_left() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.pool = [stats("b"), stats("c")]
    source.history["c"] = _buys("ETHUSDT")
    h.clock.advance(hours=6)
    await h.tracker.refresh()
    view = h.tracker.view()
    assert (view.selected, view.fresh) == (2, 2)
    assert set(view.backings) == {"SOLUSDT", "ETHUSDT"}  # a's purchase no longer counts
    assert view.backings["SOLUSDT"].buyers == ("b",)
    assert h.alerts[-1] == (
        Severity.INFO,
        f"{PROFILE}: traders acompanhados: lead-b (0.58), lead-c (0.58); saíram: lead-a",
    )
    # the same traders again: recorded, not announced
    h.clock.advance(hours=6)
    await h.tracker.refresh()
    assert len(h.events(SELECTED_EVENT)) == 3 and len(h.alerts) == 2


async def test_nobody_qualifying_is_recorded_and_no_entry_goes_out() -> None:
    falling = [float(90 - day) for day in range(90)]
    source = FakeSource([stats("a")], curves={"a": falling}, trades={"a": active()})
    h = Harness(source)
    await h.tracker.refresh()
    assert h.tracker.view().status == NO_LEADS
    (selected,) = h.events(SELECTED_EVENT)
    assert selected["selected"] == [] and WEAK_RECENT in selected["rejected"]
    assert h.alerts == []  # nobody followed before either: nothing changed
    # and when the followed ones all leave, that is announced
    kept = Harness(_source(a=_buys()))
    await kept.tracker.refresh()
    kept.source.pool = []
    kept.clock.advance(hours=6)
    await kept.tracker.refresh()
    assert kept.alerts[-1][1] == f"{PROFILE}: traders acompanhados: nenhum; saíram: lead-a"


async def test_the_ranking_reads_several_pages_until_a_short_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracker_module, "PAGE_SIZE", 2)
    source = FakeSource([stats(str(n), roi_pct=0.0) for n in range(5)], page_size=2)
    h = Harness(source)
    await h.tracker.refresh()
    assert source.calls == ["ranking:1", "ranking:2", "ranking:3"]  # the third has one row
    assert h.events(SELECTED_EVENT)[0]["pool"] == 5


async def test_the_ranking_stops_at_the_configured_pages() -> None:
    config = LeadsConfig.model_validate({"selection": {"pool_pages": 2}})
    source = FakeSource([stats(str(n), roi_pct=0.0) for n in range(90)])
    h = Harness(source, config=config)
    await h.tracker.refresh()
    assert source.calls == ["ranking:1", "ranking:2"]


async def test_a_failed_ranking_keeps_the_followed_traders_until_it_is_too_old() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.fail("ranking")
    h.clock.advance(hours=6)
    with capture_logs() as logs:
        await h.tracker.refresh()
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["leads.ranking_failed"]
    view = h.tracker.view()
    assert (view.status, view.fresh) == (OK, 2)  # their trades were still read
    assert len(h.events(SELECTED_EVENT)) == 1
    assert h.events(UNAVAILABLE_EVENT) == []  # part of the reading worked
    # tried again at every reading, and past max_ranking_age nobody is followed
    source.calls.clear()
    h.clock.advance(minutes=15)
    await h.tracker.refresh()
    assert source.calls[0] == "ranking:1"
    h.clock.advance(hours=18)
    await h.tracker.refresh()
    assert h.tracker.view().status == STALE_RANKING
    assert h.tracker.view().backings == {}


async def test_a_candidate_that_cannot_be_read_is_left_out_of_that_ranking() -> None:
    """A portfolio closed since the ranking answers an error: it must not cost the others."""
    source = _source(a=_buys(), b=_buys(), c=_buys())
    source.fail("curve:b", LeadSourceError("resposta 11081031: The current portfolio is closed."))
    h = Harness(source)
    with capture_logs() as logs:
        await h.tracker.refresh()
    (failed,) = [e for e in logs if e["event"] == "leads.candidate_failed"]
    assert (failed["lead"], "11081031" in failed["error"]) == ("b", True)
    (selected,) = h.events(SELECTED_EVENT)
    assert [lead["id"] for lead in selected["selected"]] == ["a", "c"]
    assert selected["rejected"] == {UNREADABLE: 1}
    assert h.tracker.view().backings["SOLUSDT"].buyers == ("a", "c")
    assert h.events(UNAVAILABLE_EVENT) == []


async def test_a_ranking_where_no_candidate_can_be_read_keeps_the_followed_traders() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.fail("curve")  # the ranking pages answer, the portfolios' pages do not
    h.clock.advance(hours=6)
    with capture_logs() as logs:
        await h.tracker.refresh()
    failed = [e["event"] for e in logs if e["log_level"] == "warning"]
    assert failed == ["leads.candidate_failed", "leads.candidate_failed", "leads.ranking_failed"]
    assert len(h.events(SELECTED_EVENT)) == 1  # not replaced by an empty selection
    view = h.tracker.view()
    assert (view.status, view.selected, view.fresh) == (OK, 2, 2)


async def test_a_trader_that_cannot_be_read_stops_counting_after_max_data_age() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.fail("trades:b")
    h.clock.advance(minutes=15)
    with capture_logs() as logs:
        await h.tracker.refresh()
    (failed,) = [e for e in logs if e["event"] == "leads.trades_failed"]
    assert (failed["lead"], failed["error"]) == ("b", "HTTP 500")
    view = h.tracker.view()
    assert (view.fresh, view.as_of) == (2, NOW)  # b's reading of 15 min ago still counts
    assert view.backings["SOLUSDT"].buyers == ("a", "b")
    h.clock.advance(minutes=45)
    await h.tracker.refresh()
    view = h.tracker.view()
    assert (view.selected, view.fresh) == (2, 1)  # b's reading is an hour old
    assert view.backings["SOLUSDT"].buyers == ("a",)
    assert view.backings["SOLUSDT"].consensus(2) is None


async def test_a_source_that_is_down_alerts_once_and_then_says_when_it_is_back() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.fail("trades")
    for _ in range(4):  # an hour of failures
        h.clock.advance(minutes=15)
        await h.tracker.refresh()
    assert h.events(UNAVAILABLE_EVENT) == [{"profile": PROFILE}]
    assert h.alerts[-1] == (
        Severity.HIGH,
        f"{PROFILE}: fonte de copy trading indisponível: sem novas entradas até voltar",
    )
    assert h.tracker.view().status == NO_DATA
    assert h.tracker.view().selected == 2
    source.failures.clear()
    h.clock.advance(minutes=15)
    await h.tracker.refresh()
    assert h.events(RECOVERED_EVENT) == [{"profile": PROFILE}]
    assert h.alerts[-1] == (Severity.INFO, f"{PROFILE}: fonte de copy trading voltou a responder")
    assert h.tracker.view().status == OK
    severities = [call.args[1] for call in h.store.record_event.await_args_list[-2:]]
    assert severities == [Severity.HIGH, Severity.INFO]


async def test_a_refusal_stops_every_request_for_the_backoff() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.fail("trades:a", LeadSourceRefusedError("HTTP 429"))
    source.calls.clear()
    h.clock.advance(minutes=15)
    with capture_logs() as logs:
        await h.tracker.refresh()
    assert source.calls == ["trades:a"]  # b was not even asked
    (refused,) = [e for e in logs if e["event"] == "leads.source_refused"]
    assert refused["until"] == (h.clock.now + timedelta(hours=1)).isoformat()
    assert len(h.events(UNAVAILABLE_EVENT)) == 1
    source.failures.clear()
    for _ in range(3):  # 45 minutes: still backing off
        h.clock.advance(minutes=15)
        await h.tracker.refresh()
    assert source.calls == ["trades:a"]
    h.clock.advance(minutes=15)
    await h.tracker.refresh()
    assert source.calls == ["trades:a", "trades:a", "trades:b"]
    assert len(h.events(RECOVERED_EVENT)) == 1


async def test_a_refusal_during_the_ranking_keeps_the_followed_traders() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source)
    await h.tracker.refresh()
    source.fail("curve", LeadSourceRefusedError("HTTP 418"))
    h.clock.advance(hours=6)
    await h.tracker.refresh()
    assert h.tracker.view(h.clock.now).selected == 2
    assert len(h.events(SELECTED_EVENT)) == 1


async def test_the_followed_traders_survive_a_restart() -> None:
    first = Harness(_source(a=_buys(), b=_buys()))
    await first.tracker.refresh()
    saved = first.store.set_checkpoint.await_args.args[1]

    source = _source(a=_buys(), b=_buys())
    source.fail("ranking")  # the ranking is not due anyway
    restarted = Harness(source, store=_store(saved))
    restarted.clock.advance(hours=1)
    await restarted.tracker.refresh()
    restarted.store.get_checkpoint.assert_awaited_once_with(KEY)
    assert source.calls == ["trades:a", "trades:b"]
    view = restarted.tracker.view()
    assert (view.status, view.selected, view.fresh) == (OK, 2, 2)
    assert view.backings["SOLUSDT"].buyers == ("a", "b")
    assert restarted.events(SELECTED_EVENT) == [] and restarted.alerts == []
    await restarted.tracker.refresh()
    restarted.store.get_checkpoint.assert_awaited_once()  # loaded once


async def test_an_unreadable_checkpoint_is_ignored() -> None:
    source = _source(a=_buys())
    h = Harness(source, store=_store({"ranked_at": "yesterday", "leads": []}))
    with capture_logs() as logs:
        await h.tracker.refresh()
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
        "leads.checkpoint_unreadable"
    ]
    assert h.tracker.view().selected == 1  # ranked from scratch


async def test_without_a_notifier_the_tracker_only_records() -> None:
    source = _source(a=_buys(), b=_buys())
    h = Harness(source, notify=False)
    await h.tracker.refresh()
    source.fail("trades")
    h.clock.advance(minutes=15)
    await h.tracker.refresh()
    assert len(h.events(SELECTED_EVENT)) == 1 and len(h.events(UNAVAILABLE_EVENT)) == 1


async def test_a_reading_that_asks_nothing_changes_nothing() -> None:
    """Nobody followed and the ranking not due: no request, so no verdict on the source."""
    h = Harness(_source())
    await h.tracker.refresh()
    assert h.tracker.view().status == NO_LEADS
    h.source.fail("ranking")
    h.clock.advance(hours=6)
    await h.tracker.refresh()  # down
    h.source.failures.clear()
    h.store.get_checkpoint.return_value = None
    h.clock.advance(minutes=15)
    await h.tracker.refresh()  # ranks again, finds nobody: one request, the source is back
    assert len(h.events(UNAVAILABLE_EVENT)) == 1 and len(h.events(RECOVERED_EVENT)) == 1
    h.clock.advance(minutes=15)
    h.source.calls.clear()
    await h.tracker.refresh()
    assert h.source.calls == []
    assert len(h.events(RECOVERED_EVENT)) == 1


def test_the_task_runs_at_the_poll_interval() -> None:
    config = LeadsConfig.model_validate({"poll_interval": "30m", "max_data_age": "90m"})
    assert Harness(_source(), config=config).tracker.interval_s == 1800.0
    assert Harness(_source()).tracker.interval_s == 900.0


def test_a_followed_trader_round_trips_through_the_checkpoint() -> None:
    followed = Followed("7", "ana", D("1234.5"), 0.61, {"roi_pct": 12.0, "sharpe": None})
    data = followed.to_json()
    assert data == {
        "id": "7", "nickname": "ana", "balance": "1234.5", "score": 0.61,
        "roi_pct": 12.0, "sharpe": None,
    }  # fmt: skip
    assert Followed.from_json(data) == followed
    with pytest.raises(KeyError):
        Followed.from_json({"id": "7"})
