"""The A/B tape's recorder, with the exchange and the database simulated."""

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lab.paper import CANDIDATES as LAB_CANDIDATES
from structlog.testing import capture_logs

from tests.unit.strategy.test_portfolio_and_exits import _candidate
from trade_agent.decision.tape import CANDIDATES, WATCH_MARGIN, DecisionTape, watch_window
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.models import BookTicker
from trade_agent.persistence.store import Store
from trade_agent.risk.state import OpState
from trade_agent.signals import Signal
from trade_agent.strategy.portfolio import Candidate, MarketOpinion
from trade_agent.strategy.profiles import StrategyConfig, load_strategy_config

D = Decimal
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "profiles.yaml"
WINDOW = timedelta(days=19)
RETENTION = timedelta(days=120)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _book(symbol: str, bid: str = "100", ask: str = "101") -> BookTicker:
    return BookTicker.model_validate(
        {"symbol": symbol, "bidPrice": bid, "bidQty": "1", "askPrice": ask, "askQty": "1"}
    )


def _store(setups: dict[str, datetime] | None = None, held: Iterable[str] = ()) -> AsyncMock:
    store = AsyncMock(spec=Store)
    store.tape_setup_symbols.return_value = setups or {}
    store.active_positions.return_value = [SimpleNamespace(symbol=s) for s in held]
    store.add_tape_quotes.side_effect = lambda _at, quotes: len(quotes)
    store.prune_tape_quotes.return_value = 0
    return store


def _api(*books: BookTicker) -> AsyncMock:
    api = AsyncMock(spec=BinanceSpotApi)
    api.book_tickers.return_value = list(books)
    return api


def _tape(
    store: AsyncMock, api: AsyncMock | None = None, clock: Clock | None = None
) -> DecisionTape:
    return DecisionTape(
        api=api or _api(),
        store=store,
        window=WINDOW,
        retention=RETENTION,
        clock=clock or Clock(),
    )


def _written(store: AsyncMock) -> set[str]:
    """The symbols of the last quote batch."""
    return {symbol for symbol, _, _ in store.add_tape_quotes.call_args.args[1]}


# ------------------------------------------------------------------ the watch window
def test_the_watch_window_is_the_longest_holding_of_the_enabled_profiles_plus_a_day() -> None:
    strategy = load_strategy_config(FIXTURE)
    assert watch_window(strategy, retention=RETENTION) == timedelta(days=21) + WATCH_MARGIN
    disabled = strategy.profiles["agressivo"]
    assert not disabled.enabled
    longest = disabled.protection.model_copy(update={"max_holding": timedelta(days=90)})
    edited = disabled.model_copy(update={"protection": longest})
    profiles = {**strategy.profiles, "agressivo": edited}
    longer = strategy.model_copy(update={"profiles": profiles})
    assert watch_window(longer, retention=RETENTION) == timedelta(days=21) + WATCH_MARGIN


def _without_holding(strategy: StrategyConfig) -> StrategyConfig:
    profile = strategy.profiles["moderado"]
    protection = profile.protection.model_copy(update={"max_holding": None})
    edited = profile.model_copy(update={"protection": protection})
    return strategy.model_copy(update={"profiles": {**strategy.profiles, "moderado": edited}})


def test_without_a_maximum_holding_the_window_is_the_quotes_retention() -> None:
    strategy = load_strategy_config(FIXTURE)
    assert watch_window(_without_holding(strategy), retention=RETENTION) == RETENTION
    none = strategy.model_copy(update={"profiles": {}})
    assert watch_window(none, retention=RETENTION) == RETENTION


# ------------------------------------------------------------------ serialization
def _full_candidates() -> list[Candidate]:
    vetoed = _candidate(
        "SOLUSDT", opinion=MarketOpinion(sentiment=D("-0.9"), confidence=D("0.85"), veto=True)
    )
    local = Candidate(
        member=vetoed.member,
        signal=Signal(
            score=0.65,
            setup="pair_breakout",
            stop_pct=0.031,
            atr_pct=1e-05,
            close=99.5,
            target_pct=0.05,
            max_entry_price=101.25,
            level=99.0,
            exit_signal=True,
            filtered="regime",
            local_regime="alta",
        ),
        bid=D("0.00001234"),
        ask=D("0.00001240"),
    )
    return [vetoed, local, _candidate("ETHUSDT", setup=None)]


def test_the_serialized_candidates_round_trip_through_the_labs_schema() -> None:
    candidates = _full_candidates()
    stored = json.loads(json.dumps(CANDIDATES.dump_python(candidates, mode="json")))
    assert LAB_CANDIDATES.validate_python(stored) == candidates
    assert CANDIDATES.dump_python(candidates, mode="json") == LAB_CANDIDATES.dump_python(
        candidates, mode="json"
    )


# ------------------------------------------------------------------ decisions
async def test_a_decision_is_persisted_and_watches_only_its_setups() -> None:
    store, clock = _store(), Clock()
    api = _api(_book("SOLUSDT"), _book("ETHUSDT"))
    tape = _tape(store, api, clock)
    candidates = _full_candidates()
    await tape.record_decision(
        profile="conservador",
        at=NOW,
        state=OpState.PAUSED,
        research="ok",
        exposure_multiplier=D("0.5"),
        candidates=candidates,
    )
    kwargs = store.add_tape_decision.call_args.kwargs
    assert (kwargs["at"], kwargs["profile"], kwargs["state"]) == (NOW, "conservador", "paused")
    assert (kwargs["research"], kwargs["exposure_multiplier"]) == ("ok", D("0.5"))
    assert LAB_CANDIDATES.validate_python(kwargs["candidates"]) == candidates
    assert await tape.record_quotes() == 1
    assert _written(store) == {"SOLUSDT"}  # ETHUSDT has no setup


async def test_a_decision_that_was_not_persisted_watches_nothing() -> None:
    """The watch-list mirrors the tape: a paper account cannot hold what was never recorded."""
    store = _store()
    store.add_tape_decision.side_effect = OSError("db down")
    api = _api(_book("SOLUSDT"))
    tape = _tape(store, api)
    with pytest.raises(OSError, match="db down"):
        await tape.record_decision(
            profile="conservador",
            at=NOW,
            state=OpState.RUNNING,
            research="ok",
            exposure_multiplier=D(1),
            candidates=[_candidate("SOLUSDT")],
        )
    assert await tape.record_quotes() == 0
    api.book_tickers.assert_not_awaited()


# ------------------------------------------------------------------ quote ticks
async def test_a_tick_records_the_watch_list_only_with_one_request() -> None:
    store = _store(setups={"SOLUSDT": NOW - timedelta(days=2)}, held=["XRPUSDT"])
    api = _api(_book("SOLUSDT", "100", "100.5"), _book("XRPUSDT"), _book("BTCUSDT"))
    tape = _tape(store, api)
    assert await tape.record_quotes() == 2
    api.book_tickers.assert_awaited_once()
    at, quotes = store.add_tape_quotes.call_args.args
    assert at == NOW
    assert quotes == [("SOLUSDT", D(100), D("100.5")), ("XRPUSDT", D(100), D(101))]


async def test_a_setup_out_of_the_window_stops_being_recorded_unless_held() -> None:
    clock = Clock()
    store = _store(setups={"SOLUSDT": NOW, "ADAUSDT": NOW}, held=["ADAUSDT"])
    api = _api(_book("SOLUSDT"), _book("ADAUSDT"))
    tape = _tape(store, api, clock)
    await tape.record_quotes()
    assert _written(store) == {"SOLUSDT", "ADAUSDT"}
    clock.now = NOW + WINDOW  # the very edge of the window still counts
    await tape.record_quotes()
    assert _written(store) == {"SOLUSDT", "ADAUSDT"}
    clock.now = NOW + WINDOW + timedelta(minutes=1)
    await tape.record_quotes()
    assert _written(store) == {"ADAUSDT"}
    store.active_positions.return_value = []
    api.book_tickers.reset_mock()
    assert await tape.record_quotes() == 0
    api.book_tickers.assert_not_awaited()  # nothing left to watch: no request


async def test_a_new_setup_extends_the_window_of_its_symbol() -> None:
    clock = Clock()
    store = _store(setups={"SOLUSDT": NOW})
    api = _api(_book("SOLUSDT"))
    tape = _tape(store, api, clock)
    await tape.record_quotes()
    clock.now = NOW + WINDOW
    await tape.record_decision(
        profile="conservador",
        at=clock.now,
        state=OpState.RUNNING,
        research="ok",
        exposure_multiplier=D(1),
        candidates=[_candidate("SOLUSDT")],
    )
    clock.now += timedelta(days=1)
    assert await tape.record_quotes() == 1


async def test_a_book_with_an_empty_side_or_crossed_is_no_quote() -> None:
    """The replay raises on these, which would refuse the whole tape."""
    store = _store(held=["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"])
    api = _api(
        _book("AAAUSDT", "0", "0"),
        _book("BBBUSDT", "0", "1"),
        _book("CCCUSDT", "2", "1"),
        _book("DDDUSDT", "1", "1"),
    )
    assert await _tape(store, api).record_quotes() == 1
    assert _written(store) == {"DDDUSDT"}


async def test_the_watch_list_is_rebuilt_once_and_again_after_a_failure() -> None:
    clock = Clock()
    store = _store()
    store.tape_setup_symbols.side_effect = [OSError("db down"), {"SOLUSDT": NOW}]
    api = _api(_book("SOLUSDT"))
    tape = _tape(store, api, clock)
    with capture_logs() as logs:
        assert await tape.record_quotes() == 0
    (failure,) = [e for e in logs if e["event"] == "tape.quotes_failed"]
    assert failure["log_level"] == "warning" and "db down" in failure["error"]
    store.add_tape_quotes.assert_not_awaited()
    assert await tape.record_quotes() == 1  # the next tick tries again
    assert await tape.record_quotes() == 1
    assert store.tape_setup_symbols.await_count == 2  # and never reads it again
    store.tape_setup_symbols.assert_awaited_with(NOW - WINDOW)


async def test_a_rebuild_does_not_undo_a_later_setup() -> None:
    """A cycle that closes before the first tick: the rebuild's older reading does not win."""
    clock = Clock()
    store = _store(setups={"SOLUSDT": NOW - WINDOW})
    api = _api(_book("SOLUSDT"))
    tape = _tape(store, api, clock)
    await tape.record_decision(
        profile="conservador",
        at=NOW,
        state=OpState.RUNNING,
        research="ok",
        exposure_multiplier=D(1),
        candidates=[_candidate("SOLUSDT")],
    )
    await tape.record_quotes()
    clock.now = NOW + timedelta(days=1)
    assert await tape.record_quotes() == 1


async def test_quotes_are_pruned_once_per_utc_day() -> None:
    clock = Clock()
    store = _store()
    tape = _tape(store, clock=clock)
    await tape.record_quotes()
    store.prune_tape_quotes.assert_awaited_once_with(NOW - RETENTION)
    clock.now = NOW + timedelta(hours=11, minutes=59)  # 23:59 UTC: the same day
    await tape.record_quotes()
    store.prune_tape_quotes.assert_awaited_once()
    clock.now = datetime(2026, 10, 21, tzinfo=UTC)
    await tape.record_quotes()
    assert store.prune_tape_quotes.await_count == 2
    store.prune_tape_quotes.assert_awaited_with(clock.now - RETENTION)


async def test_a_failed_prune_is_retried_at_the_next_tick() -> None:
    store = _store()
    store.prune_tape_quotes.side_effect = [OSError("db down"), 5]
    tape = _tape(store)
    with capture_logs() as logs:
        await tape.record_quotes()
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["tape.quotes_failed"]
    await tape.record_quotes()
    assert store.prune_tape_quotes.await_count == 2


@pytest.mark.parametrize("failing", ["exchange", "write"])
async def test_a_failed_tick_only_logs_a_warning(failing: str) -> None:
    store = _store(held=["SOLUSDT"])
    api = _api(_book("SOLUSDT"))
    if failing == "exchange":
        api.book_tickers.side_effect = OSError("no connection")
    else:
        store.add_tape_quotes.side_effect = OSError("db down")
    with capture_logs() as logs:
        assert await _tape(store, api).record_quotes() == 0
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["tape.quotes_failed"]
