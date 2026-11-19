"""Contract of the Binance Spot Copy Trading endpoints the lead tracker reads (D-052).

They are the endpoints Binance's own website calls (``www.binance.com/bapi/...``): public and
keyless, but undocumented, so nothing tells when they change. These tests call them live with
the agent's own client (:class:`~trade_agent.leads.source.LeadSource`, the same URLs, bodies and
headers) and check what the parsers need **and** the meaning the strategy relies on: the
ranking's order and units, a PnL curve of one point a day, trades whose amount is in the quote
asset, the error envelope of a closed portfolio, and the tracker choosing traders end to end.

A failure means the contract changed: the parsers in ``trade_agent/leads/source.py`` (or the
assumptions in ``ranking.py`` and ``consensus.py``) must follow it. A machine the source refuses
(HTTP 403, 418, 429 or 451) or with no connection skips the tests: there is nothing to check.
About 25 requests per run, spaced half a second apart.
"""

import asyncio
import itertools
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from structlog.testing import capture_logs

from trade_agent.leads.consensus import NO_LEADS, OK
from trade_agent.leads.source import (
    PAGE_SIZE,
    TRADES_PAGE,
    LeadSource,
    LeadSourceError,
    LeadSourceRefusedError,
)
from trade_agent.leads.tracker import SELECTED_EVENT, UNAVAILABLE_EVENT, LeadTracker
from trade_agent.persistence.store import Store
from trade_agent.strategy.profiles import load_strategy_config

ROOT = Path(__file__).parents[3]
DELAY_S = 0.5
DAY_MS = 86_400_000
PAST_THE_END = 500
"""A ranking page far past the last portfolio (about 2,200 public ones on 2026-11-19)."""
CLOSED = "1"
"""A portfolio id that does not exist: the source answers it as a closed portfolio."""
RANKING_FIELDS = frozenset(
    {"leadPortfolioId", "nickname", "roi", "mdd", "sharpRatio", "aum", "copyAum", "copierPnl"}
    | {"tradingDays", "pnl"}
)
TRADE_FIELDS = frozenset(
    {"time", "symbol", "side", "executed", "price", "totalAmount", "quoteAsset", "baseAsset"}
)


class Recorder:
    """The source's raw answers, in order, next to what ``LeadSource`` parsed from them."""

    def __init__(self) -> None:
        self.bodies: list[Any] = []

    async def __call__(self, response: httpx.Response) -> None:
        await response.aread()
        try:
            self.bodies.append(response.json())
        except ValueError:
            self.bodies.append(response.text)


@asynccontextmanager
async def source() -> AsyncIterator[tuple[LeadSource, Recorder]]:
    recorder = Recorder()
    async with httpx.AsyncClient(timeout=20, event_hooks={"response": [recorder]}) as http:
        yield LeadSource(http), recorder


async def ask[T](request: Awaitable[T]) -> T:
    """One request, spaced from the previous one; a refusal or no connection skips the test."""
    await asyncio.sleep(DELAY_S)
    try:
        return await request
    except LeadSourceRefusedError as exc:
        pytest.skip(f"the source refuses this machine ({exc}): the contract cannot be checked")
    except LeadSourceError as exc:
        if isinstance(exc.__cause__, httpx.TransportError):
            pytest.skip(f"no connection to the source: {exc}")
        raise


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# ============================================================================ the ranking
async def test_the_ranking_is_paged_by_the_best_sharpe_ratio() -> None:
    """``pool_pages`` reads the first pages as the best portfolios: 30 a page, no repeats,
    the Sharpe ratio never rising, the young ones (no Sharpe ratio) left out, and a page past
    the last portfolio empty, not an error."""
    async with source() as (leads, raw):
        first = await ask(leads.ranking(1))
        second = await ask(leads.ranking(2))
        past = await ask(leads.ranking(PAST_THE_END))
    assert len(first) == len(second) == PAGE_SIZE
    assert not {s.portfolio_id for s in first} & {s.portfolio_id for s in second}
    sharpes = [s.sharpe for s in first + second if s.sharpe is not None]
    assert len(sharpes) == 2 * PAGE_SIZE  # the young portfolios, with no Sharpe ratio, are left out
    assert all(a >= b - 1e-9 for a, b in itertools.pairwise(sharpes)), sharpes
    assert past == []
    total = raw.bodies[0]["data"]["total"]
    assert isinstance(total, int) and total > 2 * PAGE_SIZE


async def test_ranking_rows_carry_the_figures_of_the_selection() -> None:
    async with source() as (leads, raw):
        pool = await ask(leads.ranking(1))
    rows = raw.bodies[0]["data"]["list"]
    for row in rows:
        missing = RANKING_FIELDS - set(row)
        assert not missing, missing
        assert isinstance(row["leadPortfolioId"], str) and row["leadPortfolioId"].isdigit()
        assert all(_number(row[name]) for name in ("roi", "mdd", "aum", "copyAum", "copierPnl"))
        assert _number(row["sharpRatio"]) or row["sharpRatio"] is None
        assert isinstance(row["tradingDays"], int) and row["tradingDays"] >= 0
    # the units the thresholds are written in: percentages, USDT, the copiers' share of the AUM
    assert all(0 <= lead.mdd_pct <= 100 and lead.roi_pct > -100 for lead in pool)
    assert any(lead.mdd_pct > 1 for lead in pool), "drawdown no longer in %?"
    assert all(lead.aum >= lead.copy_aum >= 0 for lead in pool)  # the balance is the owner's
    assert all(lead.trading_days >= 1 for lead in pool)


# ============================================================================ a portfolio
async def test_the_pnl_curve_is_the_cumulative_pnl_of_the_90_days() -> None:
    """``positive_weeks`` and ``recent_gain_pct`` step through it day by day and divide it by
    the balance: one point a day, ascending, in USDT, ending at the ranking's own PnL."""
    async with source() as (leads, raw):
        pool = await ask(leads.ranking(1))
        row = raw.bodies[-1]["data"]["list"][0]
        curve = await ask(leads.pnl_curve(pool[0].portfolio_id))
        points = raw.bodies[-1]["data"]
    assert 80 <= len(curve) <= 91
    assert {p["dataType"] for p in points} == {"PNL"}
    assert all(_number(p["value"]) and isinstance(p["dateTime"], int) for p in points)
    times = [p["dateTime"] for p in points]
    assert all(b - a >= DAY_MS for a, b in itertools.pairwise(times))  # a day may be missing
    now_ms = datetime.now(UTC).timestamp() * 1000
    assert now_ms - times[-1] < 3 * DAY_MS
    assert [p.at for p in curve] == sorted(p.at for p in curve)
    assert curve[-1].pnl == pytest.approx(row["pnl"], rel=0.05, abs=1.0)


async def test_trades_are_the_latest_fills_with_their_amount_in_the_quote_asset() -> None:
    """``min_buy_pct`` compares ``totalAmount`` with the trader's balance in USDT: it must be
    the quote amount of the fill (quantity × price), the most recent first, 100 at most."""
    async with source() as (leads, raw):
        pool = await ask(leads.ranking(1))
        for lead in pool[:5]:
            trades = await ask(leads.trades(lead.portfolio_id))
            if trades:
                break
        rows = raw.bodies[-1]["data"]["list"]
    assert trades, "no trades in the first five portfolios of the ranking"
    assert len(trades) == len(rows) <= TRADES_PAGE
    times = [row["time"] for row in rows]
    assert times == sorted(times, reverse=True)
    for row in rows:
        missing = TRADE_FIELDS - set(row)
        assert not missing, missing
        assert row["side"] in {"BUY", "SELL"}
        assert row["symbol"] == row["baseAsset"] + row["quoteAsset"]
        quote = float(row["executed"]) * float(row["price"])
        assert float(row["totalAmount"]) == pytest.approx(quote, rel=0.01), row
    assert {t.quote_asset for t in trades} <= {"USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB"}


async def test_a_closed_portfolio_is_an_error_envelope() -> None:
    """A portfolio closed since the ranking: its trades are an error (the tracker leaves it out
    of that ranking), its PnL curve comes back empty."""
    async with source() as (leads, _):
        with pytest.raises(LeadSourceError, match="11081031"):
            await ask(leads.trades(CLOSED))
        assert await ask(leads.pnl_curve(CLOSED)) == ()


# ============================================================================ end to end
async def test_the_tracker_chooses_traders_from_the_live_source() -> None:
    """The repository's lead profile, its tracker and its settings, against the live source:
    every request parses, the ranking is read whole and the view has a reading."""
    strategy = load_strategy_config(ROOT / "config" / "profiles.yaml")
    name, profile = next((n, p) for n, p in strategy.profiles.items() if p.leads is not None)
    assert profile.leads is not None
    store = AsyncMock(spec=Store)
    store.get_checkpoint.return_value = None
    async with httpx.AsyncClient(timeout=20) as http:
        tracker = LeadTracker(
            profile=name,
            config=profile.leads,
            source=LeadSource(http),
            store=store,
            quote_asset=strategy.account.quote_asset,
        )
        with capture_logs() as logs:
            await tracker.refresh()
    refused = [e for e in logs if e["event"] == "leads.source_refused"]
    if refused:
        pytest.skip(f"the source refuses this machine ({refused[0]['error']})")
    warnings = [e for e in logs if e["log_level"] == "warning"]
    kinds = [call.args[0] for call in store.record_event.await_args_list]
    assert UNAVAILABLE_EVENT not in kinds, warnings
    assert [e["event"] for e in warnings if e["event"] != "leads.candidate_failed"] == []
    (selected,) = [
        c.args[2] for c in store.record_event.await_args_list if c.args[0] == SELECTED_EVENT
    ]
    assert selected["pool"] == profile.leads.selection.pool_pages * PAGE_SIZE
    assert tracker.view().status in {OK, NO_LEADS}
