"""The A/B tape's tables and its read-only export (isolated PostgreSQL)."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from lab.paper import CANDIDATES, PaperPortfolio, replay
from scripts.tape_export import export, main
from sqlalchemy import func, select

from tests.unit.strategy.test_portfolio_and_exits import CONFIG, _candidate
from trade_agent.persistence.db import Database
from trade_agent.persistence.models import TapeQuoteRecord
from trade_agent.persistence.store import Store
from trade_agent.risk.conditions import PreTradeConfig, StopConditions
from trade_agent.strategy.portfolio import MarketOpinion

D = Decimal
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


async def _decide(
    store: Store,
    at: datetime,
    *,
    profile: str = "conservador",
    symbols: tuple[str, ...] = ("SOLUSDT",),
    research: str = "ok",
) -> None:
    await store.add_tape_decision(
        at=at,
        profile=profile,
        state="running",
        research=research,
        exposure_multiplier=D("0.5"),
        candidates=CANDIDATES.dump_python([_candidate(s) for s in symbols], mode="json"),
    )


async def _export(db: Database, **kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kwargs = {"since": NOW - timedelta(days=1), "until": NOW + timedelta(days=1), **kwargs}
    sink = io.StringIO()
    summary = await export(db, sink, **kwargs)
    return [json.loads(line) for line in sink.getvalue().splitlines()], summary


# ------------------------------------------------------------------ store
async def test_decisions_read_a_half_open_interval_with_a_profile_filter(store: Store) -> None:
    await _decide(store, NOW + MINUTE, profile="moderado")
    await _decide(store, NOW)
    await _decide(store, NOW + 2 * MINUTE)
    rows = await store.tape_decisions(NOW, NOW + 2 * MINUTE)
    assert [(r.at, r.profile) for r in rows] == [(NOW, "conservador"), (NOW + MINUTE, "moderado")]
    only = await store.tape_decisions(NOW, NOW + 3 * MINUTE, profiles=["moderado"])
    assert [r.profile for r in only] == ["moderado"]
    (first,) = await store.tape_decisions(NOW, NOW + MINUTE)
    assert first.exposure_multiplier == D("0.5") and first.state == "running"
    assert CANDIDATES.validate_python(first.candidates) == [_candidate("SOLUSDT")]


async def test_quotes_go_in_one_statement_and_read_a_half_open_interval(
    db: Database, store: Store
) -> None:
    assert await store.add_tape_quotes(NOW, []) == 0
    written = await store.add_tape_quotes(
        NOW, [("SOLUSDT", D("0.00001234"), D("0.00001240")), ("ETHUSDT", D(2500), D("2500.01"))]
    )
    assert written == 2
    assert await store.add_tape_quotes(NOW + MINUTE, [("SOLUSDT", D(1), D(2))]) == 1
    rows = await store.tape_quotes(NOW, NOW + MINUTE)
    assert [(q.symbol, q.bid, q.ask, q.at) for q in rows] == [
        ("SOLUSDT", D("0.00001234"), D("0.00001240"), NOW),  # Numeric: never a rounded float
        ("ETHUSDT", D(2500), D("2500.01"), NOW),
    ]
    assert len(await store.tape_quotes(NOW, NOW + 2 * MINUTE)) == 3


async def test_setup_symbols_give_the_last_setup_within_the_window(store: Store) -> None:
    old, recent = NOW - timedelta(days=30), NOW - timedelta(days=2)
    await _decide(store, old, symbols=("ADAUSDT", "SOLUSDT"))
    await _decide(store, recent, symbols=("SOLUSDT",))
    await _decide(store, NOW, symbols=("SOLUSDT",))
    await store.add_tape_decision(
        at=NOW,
        profile="conservador",
        state="running",
        research="ok",
        exposure_multiplier=D(1),
        candidates=CANDIDATES.dump_python([_candidate("ETHUSDT", setup=None)], mode="json"),
    )
    assert await store.tape_setup_symbols(NOW - timedelta(days=19)) == {"SOLUSDT": NOW}
    assert await store.tape_setup_symbols(old) == {"SOLUSDT": NOW, "ADAUSDT": old}
    assert await store.tape_setup_symbols(NOW + MINUTE) == {}


async def test_pruning_removes_only_the_quotes_before_the_cutoff(
    db: Database, store: Store
) -> None:
    await store.add_tape_quotes(NOW - MINUTE, [("SOLUSDT", D(1), D(2)), ("ETHUSDT", D(1), D(2))])
    await store.add_tape_quotes(NOW, [("SOLUSDT", D(1), D(2))])
    await _decide(store, NOW - timedelta(days=500))
    assert await store.prune_tape_quotes(NOW) == 2
    assert await store.prune_tape_quotes(NOW) == 0
    async with db.session() as session:
        left = await session.scalar(select(func.count()).select_from(TapeQuoteRecord))
    assert left == 1
    assert len(await store.tape_decisions(NOW - timedelta(days=501), NOW)) == 1  # quotes only


# ------------------------------------------------------------------ export
async def test_the_export_orders_events_with_quotes_first_within_an_instant(
    db: Database, store: Store
) -> None:
    await _decide(store, NOW)
    await store.add_tape_quotes(NOW, [("SOLUSDT", D("99.5"), D("100.25"))])
    await store.add_tape_quotes(NOW - MINUTE, [("SOLUSDT", D("0.00001"), D("0.000011"))])
    await _decide(store, NOW + MINUTE, profile="moderado", research="falhou: sem conexão ")
    lines, summary = await _export(db)
    assert [(e["kind"], e["at"]) for e in lines] == [
        ("quote", (NOW - MINUTE).isoformat()),
        ("quote", NOW.isoformat()),
        ("decision", NOW.isoformat()),
        ("decision", (NOW + MINUTE).isoformat()),
    ]
    assert lines[0] == {
        "kind": "quote",
        "at": "2026-10-20T11:59:00+00:00",  # the UTC offset the replay demands
        "symbol": "SOLUSDT",
        "bid": "0.00001",  # strings in fixed notation, as the column keeps 18 places
        "ask": "0.000011",
    }
    decision = lines[2]
    assert decision["profile"] == "conservador"
    assert decision["exposure_multiplier"] == "0.5"
    assert (decision["state"], decision["research"]) == ("running", "ok")
    assert CANDIDATES.validate_python(decision["candidates"]) == [_candidate("SOLUSDT")]
    assert summary == {
        "since": (NOW - timedelta(days=1)).isoformat(),
        "until_exclusive": (NOW + timedelta(days=1)).isoformat(),
        "decisions": {"conservador": 1, "moderado": 1},
        "quotes": 2,
        "quote_symbols": 1,
        "dropped_candidates": 0,
        "first_at": (NOW - MINUTE).isoformat(),
        "last_at": (NOW + MINUTE).isoformat(),
    }


async def test_the_export_is_exact_across_its_daily_batches(db: Database, store: Store) -> None:
    await store.add_tape_quotes(NOW, [("SOLUSDT", D(1), D(2))])  # the first instant: in
    await store.add_tape_quotes(NOW + timedelta(days=1), [("SOLUSDT", D(1), D(2))])  # a boundary
    await store.add_tape_quotes(NOW + timedelta(days=2) - MINUTE, [("ETHUSDT", D(1), D(2))])
    await store.add_tape_quotes(NOW + timedelta(days=2), [("XRPUSDT", D(1), D(2))])  # the end: out
    lines, summary = await _export(db, since=NOW, until=NOW + timedelta(days=2))
    assert [e["symbol"] for e in lines] == ["SOLUSDT", "SOLUSDT", "ETHUSDT"]
    assert (summary["quotes"], summary["quote_symbols"]) == (3, 2)


async def test_the_export_filters_the_profiles(db: Database, store: Store) -> None:
    await _decide(store, NOW)
    await _decide(store, NOW, profile="moderado")
    await store.add_tape_quotes(NOW, [("SOLUSDT", D(1), D(2))])
    lines, summary = await _export(db, profiles=["moderado"])
    assert [e.get("profile") for e in lines] == [None, "moderado"]  # the quotes are shared
    assert summary["decisions"] == {"moderado": 1}


async def test_an_empty_interval_exports_nothing(db: Database) -> None:
    lines, summary = await _export(db)
    assert lines == []
    assert summary["decisions"] == {} and summary["quotes"] == 0
    assert summary["first_at"] is None and summary["last_at"] is None


async def test_the_export_drops_the_candidates_the_replay_would_refuse(
    db: Database, store: Store, tmp_path: Path
) -> None:
    """One candidate with an empty book makes the replay refuse the whole tape; the table keeps
    what the planner received."""
    good = _candidate("SOLUSDT", opinion=MarketOpinion(D(1), D(1)))
    empty, crossed = _candidate("ETHUSDT", price="0"), _candidate("XRPUSDT", price="2")
    crossed = replace(crossed, bid=D(3), ask=D(2))
    await store.add_tape_decision(
        at=NOW,
        profile="conservador",
        state="paused",
        research="ok",
        exposure_multiplier=D(1),
        candidates=CANDIDATES.dump_python([empty, good, crossed], mode="json"),
    )
    lines, summary = await _export(db)
    assert summary["dropped_candidates"] == 2
    assert CANDIDATES.validate_python(lines[0]["candidates"]) == [good]
    (row,) = await store.tape_decisions(NOW, NOW + MINUTE)
    assert len(row.candidates) == 3
    tape = tmp_path / "tape.jsonl"
    tape.write_text(json.dumps(lines[0]), encoding="utf-8")
    account = PaperPortfolio(
        CONFIG,
        StopConditions(pre_trade=PreTradeConfig(min_reward_risk=1)),
        pure_ta=True,
        fee_pct=D(".1"),
        slippage_bps=D(0),
    )
    assert replay(tape, {"ta": account})["ta"]["open_positions"] == 1


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--since",
        "2026-10-20T00:00:00Z",
        "--until",
        "2026-10-21T00:00:00Z",
        "--output",
        str(tmp_path / "tape.jsonl"),
        *extra,
    ]


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-10-20T00:00:00", "2026-10-21T00:00:00Z"),
        ("2026-10-20T00:00:00Z", "2026-10-21T00:00:00"),
        ("2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z"),
    ],
    ids=["since-naive", "until-naive", "empty-interval"],
)
def test_the_command_refuses_a_bad_interval(tmp_path: Path, since: str, until: str) -> None:
    argv = ["--since", since, "--until", until, "--output", str(tmp_path / "tape.jsonl")]
    with pytest.raises(SystemExit):
        main(argv)
    assert not list(tmp_path.iterdir())


def test_the_command_needs_the_database_and_never_leaves_a_tape_behind(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TA_LOG_LEVEL=INFO\n", encoding="utf-8")
    with pytest.raises(ValueError, match="TA_DATABASE_URL"):
        main(_args(tmp_path, "--env-file", str(env_file)))
    assert not (tmp_path / "tape.jsonl").exists()


def test_the_command_needs_no_exchange_credentials(
    postgres_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"TA_DATABASE_URL={postgres_url}\n", encoding="utf-8")
    main(_args(tmp_path, "--env-file", str(env_file), "--profiles", "conservador", "moderado"))
    summary = json.loads(capsys.readouterr().out)
    assert summary["output"] == str(tmp_path / "tape.jsonl")
    assert (tmp_path / "tape.jsonl").read_text("utf-8") == ""
