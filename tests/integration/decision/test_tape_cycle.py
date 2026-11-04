"""The A/B tape in the decision cycle (simulated Binance + PostgreSQL), through its export to the
offline replay of the pure-TA and TA + LLM accounts."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from lab.paper import CANDIDATES, PaperPortfolio, replay
from scripts import tape_export
from sqlalchemy import func, select
from structlog.testing import capture_logs

from tests.integration.decision import test_local_engine
from tests.integration.decision.test_engine import (
    Clock,
    _engine,
    _research,
    _setup_market,
    _strategy,
    _twin_profiles,
)
from tests.support.candles import pair_pullback
from tests.support.claude import FakeClaude
from tests.support.fake_binance import FakeBinance, Fault
from tests.support.risk import CONDITIONS, POLICY
from trade_agent.decision import engine as engine_module
from trade_agent.decision.tape import DecisionTape
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceError
from trade_agent.execution.orders import EntryMode, EntryOrder
from trade_agent.execution.service import PositionService
from trade_agent.persistence.db import Database
from trade_agent.persistence.models import TapeDecisionRecord, TapeQuoteRecord
from trade_agent.persistence.store import Store
from trade_agent.research.reading import MarketReading
from trade_agent.risk.state import GLOBAL
from trade_agent.strategy.portfolio import Candidate, MarketOpinion, plan_entries

D = Decimal
BOOKS = "/api/v3/ticker/bookTicker"
WINDOW = timedelta(days=22)
RETENTION = timedelta(days=120)
VETO_VIEW = {
    "market_regime": "neutral", "global_sentiment": 0, "exposure_multiplier": 0.5,
    "global_risk_flags": [],
    "assets": [{
        "asset": "SOL", "sentiment": -0.9, "confidence": 0.9, "horizon": "days",
        "catalysts": [], "risk_flags": ["exploit"], "veto": True, "rationale": "r",
        "sources": [],
    }],
}  # fmt: skip


def _tape(api: BinanceSpotApi, store: Store, clock: Clock) -> DecisionTape:
    return DecisionTape(api=api, store=store, window=WINDOW, retention=RETENTION, clock=clock)


def _all_books_calls(fake: FakeBinance) -> int:
    """Requests for every symbol's book (the break-even asks for one symbol's)."""
    return sum("symbol" not in r.url.params for r in fake.calls("GET", BOOKS))


async def _recorded(store: Store, clock: Clock) -> list[TapeDecisionRecord]:
    day = timedelta(days=1)
    return await store.tape_decisions(clock.now - day, clock.now + day)


async def _quotes(db: Database) -> list[TapeQuoteRecord]:
    async with db.session() as session:
        return list(await session.scalars(select(TapeQuoteRecord).order_by(TapeQuoteRecord.at)))


async def _count(db: Database, table: type[Any]) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(table)) or 0)


def _spy_on_the_planner(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Candidate]]:
    received: dict[str, list[Candidate]] = {}

    def spy(**kwargs: Any) -> Any:
        received[kwargs["name"]] = list(kwargs["candidates"])
        return plan_entries(**kwargs)

    monkeypatch.setattr(engine_module, "plan_entries", spy)
    return received


# ------------------------------------------------------------------ AC 1
async def test_a_cycle_records_what_the_planner_received_per_enabled_profile(
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_market(fake)
    clock = Clock()
    tape = _tape(api, store, clock)
    engine, _ = _engine(
        api, service, store, strategy=_twin_profiles(), clock=clock, dry_run=True, tape=tape
    )
    reading = MarketReading(D("0.5"), {"SOL": MarketOpinion(D("-0.9"), D("0.9"), veto=True)})
    monkeypatch.setattr(engine, "_reading", AsyncMock(return_value=(reading, "ok")))
    received = _spy_on_the_planner(monkeypatch)
    reports = [await engine.run_profile(n) for n in ("conservador", "moderado")]
    assert all(r.setups == 1 for r in reports)
    rows = await _recorded(store, clock)
    assert [r.profile for r in rows] == ["conservador", "moderado"]  # the name, not the code
    for row in rows:
        candidates = CANDIDATES.validate_python(row.candidates)
        assert candidates == received[row.profile]
        (sol,) = [c for c in candidates if c.member.symbol == "SOLUSDT"]
        assert sol.opinion == MarketOpinion(D("-0.9"), D("0.9"), veto=True)
        assert sol.signal.setup == "trend_pullback"
        assert len(candidates) == 2  # with BTC, which has no setup: every candidate is recorded
        assert row.exposure_multiplier == reading.exposure_multiplier
        assert (row.state, row.research, row.at) == ("running", "ok", clock.now)


async def test_a_pair_local_cycle_round_trips_its_signal_fields(
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_market(fake)
    fake.candles[("SOLUSDT", "4h")] = test_local_engine._sol(pair_pullback())
    test_local_engine._falling_btc(fake)
    clock = Clock()
    engine, _ = _engine(
        api,
        service,
        store,
        strategy=test_local_engine._strategy(),
        dry_run=True,
        clock=clock,
        tape=_tape(api, store, clock),
    )
    received = _spy_on_the_planner(monkeypatch)
    report = await engine.run_profile("local")
    assert report.opened == ("SOLUSDT",)
    (row,) = await _recorded(store, clock)
    candidates = CANDIDATES.validate_python(row.candidates)
    assert candidates == received["local"]
    (sol,) = [c for c in candidates if c.member.symbol == "SOLUSDT"]
    assert sol.signal.setup == "pair_pullback" and sol.signal.local_regime == "alta"
    assert row.research == report.research


# ------------------------------------------------------------------ AC 2
@pytest.mark.parametrize(("action", "stored"), [("pause", "paused"), ("halt", "halted")])
async def test_a_blocked_cycle_is_recorded_without_any_entry(
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
    action: str,
    stored: str,
) -> None:
    _setup_market(fake)
    clock = Clock()
    engine, guard = _engine(api, service, store, clock=clock, tape=_tape(api, store, clock))
    await getattr(guard, action)(GLOBAL)
    report = await engine.run_profile("conservador")
    assert report.opened == () and report.setups == 1
    assert not fake.calls("POST", "/api/v3/orderList/opoco")
    (row,) = await _recorded(store, clock)
    assert row.state == stored
    (sol,) = [c for c in row.candidates if c["member"]["symbol"] == "SOLUSDT"]
    assert sol["signal"]["setup"] == "trend_pullback"


# ------------------------------------------------------------------ AC 3
async def test_the_quote_tape_follows_the_watch_list_and_its_retention(
    db: Database, api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    clock = Clock()
    start = clock.now
    tape = _tape(api, store, clock)
    assert await tape.record_quotes() == 0  # nothing to watch yet
    engine, _ = _engine(api, service, store, clock=clock, dry_run=True, tape=tape)
    await engine.run_profile("conservador")  # SOLUSDT's setup; BTC and ETH have none
    held = EntryOrder("BTCUSDT", D("0.001"), D(50000), EntryMode.LIMIT_MAKER_GTC)
    await service.open_position(profile="con", entry=held, policy=POLICY)
    assert await tape.record_quotes() == 2
    assert {(q.symbol, q.bid, q.ask) for q in await _quotes(db)} == {
        ("SOLUSDT", D(150), D(150)),
        ("BTCUSDT", D(63000), D(63000)),
    }
    later = clock.now = start + WINDOW + timedelta(minutes=1)  # the setup's window is over
    assert await tape.record_quotes() == 1
    assert (await _quotes(db))[-1].symbol == "BTCUSDT"  # the held one goes on
    clock.now = start + RETENTION + timedelta(minutes=1)  # another UTC day: it prunes
    assert await tape.record_quotes() == 1
    assert [(q.symbol, q.at) for q in await _quotes(db)] == [
        ("BTCUSDT", later),
        ("BTCUSDT", clock.now),
    ]


async def test_a_restart_rebuilds_the_watch_list_from_the_tape(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    clock = Clock()
    before = _tape(api, store, clock)
    engine, _ = _engine(api, service, store, clock=clock, dry_run=True, tape=before)
    await engine.run_profile("conservador")
    clock.now += WINDOW - timedelta(minutes=1)
    assert await _tape(api, store, clock).record_quotes() == 1  # a new process
    clock.now += timedelta(minutes=2)
    assert await _tape(api, store, clock).record_quotes() == 0  # out of the window


# ------------------------------------------------------------------ AC 4
async def test_a_failing_decision_record_does_not_cost_the_cycle(
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_market(fake)
    clock = Clock()
    engine, _ = _engine(api, service, store, clock=clock, tape=_tape(api, store, clock))
    monkeypatch.setattr(store, "add_tape_decision", AsyncMock(side_effect=OSError("db down")))
    with capture_logs() as logs:
        report = await engine.run_profile("conservador")
    assert report.opened == ("SOLUSDT",)
    assert len(fake.calls("POST", "/api/v3/orderList/opoco")) == 1
    assert "decision.cycle" in [e.kind for e in await store.recent_events()]
    (failure,) = [e for e in logs if e["event"] == "decision.tape_failed"]
    assert failure["log_level"] == "warning" and "db down" in failure["error"]
    assert failure["profile"] == "conservador"


async def test_a_failing_quote_tick_does_not_reach_the_runtime(
    api: BinanceSpotApi,
    store: Store,
    fake: FakeBinance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_market(fake)
    tape = _tape(api, store, Clock())
    monkeypatch.setattr(store, "tape_setup_symbols", AsyncMock(side_effect=OSError("db down")))
    with capture_logs() as logs:
        assert await tape.record_quotes() == 0
    (failure,) = [e for e in logs if e["event"] == "tape.quotes_failed"]
    assert failure["log_level"] == "warning" and "db down" in failure["error"]


async def test_failing_books_while_paused_do_not_cost_the_cycle(
    db: Database, api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """Without the tape a paused cycle reads no book: its failure cannot end what never needed
    them."""
    _setup_market(fake)
    clock = Clock()
    engine, guard = _engine(api, service, store, clock=clock, tape=_tape(api, store, clock))
    await guard.pause(GLOBAL)
    await engine._universe.get()  # the universe reads the books too: built before the fault
    fake.inject(Fault("GET", BOOKS, "reject", code=-1003, message="Too many requests."))
    with capture_logs() as logs:
        report = await engine.run_profile("conservador")
    assert report.opened == ()
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["decision.tape_failed"]
    assert "decision.cycle" in [e.kind for e in await store.recent_events()]
    assert await _count(db, TapeDecisionRecord) == 0


async def test_failing_books_with_entries_allowed_still_fail_the_cycle(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    """Unchanged: without a price there is no purchase, and the runtime records the failure."""
    _setup_market(fake)
    clock = Clock()
    engine, _ = _engine(api, service, store, clock=clock, tape=_tape(api, store, clock))
    await engine._universe.get()
    fake.inject(Fault("GET", BOOKS, "reject", code=-1003, message="Too many requests."))
    with pytest.raises(BinanceError):
        await engine.run_profile("conservador")


# ------------------------------------------------------------------ AC 5
async def test_the_exported_tape_replays_the_veto_and_the_stop(
    db: Database,
    postgres_url: str,
    api: BinanceSpotApi,
    service: PositionService,
    store: Store,
    fake: FakeBinance,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A vetoed setup: the pure-TA account buys it and the TA + LLM one does not; a later quote
    below the stop closes it (like ``test_independent_accounts_veto_and_gap_costs``)."""
    _setup_market(fake)
    clock = Clock()
    start = clock.now
    tape = _tape(api, store, clock)
    strategy = _strategy()
    claude = FakeClaude()
    claude.reply_json(VETO_VIEW)
    async with httpx.AsyncClient() as http:
        engine, _ = _engine(
            api,
            service,
            store,
            strategy=strategy,
            research=_research(db, http, claude),
            clock=clock,
            tape=tape,
        )
        report = await engine.run_profile("conservador")
    assert report.opened == () and ("SOLUSDT", "veto do analista") in report.rejected
    assert not fake.calls("POST", "/api/v3/orderList/opoco")
    for price in (D(151), D(149), D(100)):  # the stop is at most 8% away: 100 is below it
        clock.now += timedelta(minutes=1)
        fake.set_price("SOLUSDT", price)
        assert await tape.record_quotes() == 1

    env_file = tmp_path / ".env"
    env_file.write_text(f"TA_DATABASE_URL={postgres_url}\n", encoding="utf-8")
    output = tmp_path / "var" / "tape.jsonl"
    capsys.readouterr()  # the cycle's logs: from here on, only the command's summary
    await asyncio.to_thread(
        tape_export.main,
        [
            "--env-file",
            str(env_file),
            "--since",
            start.isoformat(),
            "--until",
            (clock.now + timedelta(minutes=1)).isoformat(),
            "--output",
            str(output),
        ],
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["decisions"] == {"conservador": 1}
    assert (summary["quotes"], summary["quote_symbols"]) == (3, 1)
    assert not output.with_name("tape.jsonl.partial").exists()
    assert [json.loads(line)["kind"] for line in output.read_text("utf-8").splitlines()] == [
        "decision",
        "quote",
        "quote",
        "quote",
    ]

    accounts = {
        name: PaperPortfolio(
            strategy, CONDITIONS, pure_ta=pure, fee_pct=D("0.1"), slippage_bps=D(0)
        )
        for name, pure in (("pure_ta", True), ("ta_llm", False))
    }
    result = replay(output, accounts)
    (trade,) = result["pure_ta"]["trades"]
    assert (trade["symbol"], trade["reason"]) == ("SOLUSDT", "stop")
    assert D(trade["pnl"]) < 0
    assert result["ta_llm"]["trades"] == [] and result["ta_llm"]["pnl"] == 0
    assert not accounts["ta_llm"].positions


# ------------------------------------------------------------------ AC 6 and 7
async def test_with_the_tape_off_nothing_is_written_and_no_request_is_added(
    db: Database, api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    engine, guard = _engine(api, service, store, dry_run=True)  # the setting off: no tape
    await engine._universe.get()
    universe_only = _all_books_calls(fake)
    await guard.pause(GLOBAL)
    await engine.run_profile("conservador")
    assert _all_books_calls(fake) == universe_only  # paused: no book, as before the tape
    await guard.resume(GLOBAL)
    assert (await engine.run_profile("conservador")).opened == ("SOLUSDT",)
    assert _all_books_calls(fake) == universe_only + 1
    assert await _count(db, TapeDecisionRecord) == 0
    assert await _count(db, TapeQuoteRecord) == 0


async def test_the_tape_adds_no_request_to_a_cycle_and_one_per_quote_tick(
    api: BinanceSpotApi, service: PositionService, store: Store, fake: FakeBinance
) -> None:
    _setup_market(fake)
    clock = Clock()
    tape = _tape(api, store, clock)
    engine, guard = _engine(api, service, store, clock=clock, dry_run=True, tape=tape)
    await engine._universe.get()
    calls = _all_books_calls(fake)
    await engine.run_profile("conservador")
    assert _all_books_calls(fake) == calls + 1  # the cycle's own, as before the tape
    await guard.pause(GLOBAL)
    await engine.run_profile("conservador")
    assert _all_books_calls(fake) == calls + 2  # paused: the same one, now read for the tape
    await tape.record_quotes()
    assert _all_books_calls(fake) == calls + 3
