"""The A/B tape (D-050): what ``lab.paper`` replays for the pure-TA and TA + LLM accounts.

Two records, both in PostgreSQL. A decision row per profile per cycle, with every candidate
the entry planner received, written whatever the live state, because the paper accounts apply
their own risk checks. And bid/ask quotes at the risk check's cadence, for a bounded watch-list:
the symbols a paper account could hold, which are unknown here. The proxy is every symbol with
a setup in a recorded decision within the watch window, plus the agent's active positions.

The tape is an instrument: its failures log a warning and never cost a cycle or an entry.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import structlog
from pydantic import TypeAdapter

from trade_agent import tracing
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.persistence.store import Store
from trade_agent.risk.state import OpState
from trade_agent.strategy.portfolio import Candidate
from trade_agent.strategy.profiles import StrategyConfig

log = structlog.get_logger(__name__)

CANDIDATES = TypeAdapter(list[Candidate])
"""The same schema as ``lab.paper.CANDIDATES`` (the agent does not import the lab)."""
WATCH_MARGIN = timedelta(days=1)


def watch_window(strategy: StrategyConfig, *, retention: timedelta) -> timedelta:
    """How long a setup keeps its symbol watched: the replay closes a paper position by
    ``max_holding`` at the next decision of its profile, so the longest one plus a day covers
    every paper exit. Without a maximum, a paper position may outlive any window: the quotes'
    retention is the longest that could still be replayed."""
    holdings = [p.protection.max_holding for p in strategy.enabled_profiles().values()]
    if not holdings or None in holdings:
        return retention
    return max(h for h in holdings if h is not None) + WATCH_MARGIN


class DecisionTape:
    def __init__(
        self,
        *,
        api: BinanceSpotApi,
        store: Store,
        window: timedelta,
        retention: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api = api
        self._store = store
        self._window = window
        self._retention = retention
        self._clock = clock
        self._last_setup: dict[str, datetime] = {}
        self._rebuilt = False
        self._pruned_on: date | None = None

    async def record_decision(
        self,
        *,
        profile: str,
        at: datetime,
        state: OpState,
        research: str,
        exposure_multiplier: Decimal,
        candidates: Sequence[Candidate],
    ) -> None:
        """Persists the cycle and adds its setups to the watch-list: what a rebuild would find."""
        await self._store.add_tape_decision(
            at=at,
            profile=profile,
            state=state.value,
            research=research,
            exposure_multiplier=exposure_multiplier,
            candidates=CANDIDATES.dump_python(list(candidates), mode="json"),
        )
        for candidate in candidates:
            if candidate.signal.setup is not None:
                self._note_setup(candidate.member.symbol, at)

    @tracing.traced("decision", "tape.quotes")
    async def record_quotes(self) -> int:
        """A quote tick: one ``book_tickers()`` (none with nothing to watch), filtered to the
        watch-list. Returns how many rows it wrote."""
        try:
            now = self._clock()
            written = await self._write_quotes(await self._watch_list(now))
            await self._prune(now)
        except Exception as exc:  # an instrument: the next tick tries again
            log.warning("tape.quotes_failed", error=repr(exc))
            return 0
        tracing.annotate(quotes=written)
        return written

    def _note_setup(self, symbol: str, at: datetime) -> None:
        self._last_setup[symbol] = max(at, self._last_setup.get(symbol, at))

    async def _watch_list(self, now: datetime) -> set[str]:
        cutoff = now - self._window
        if not self._rebuilt:  # first tick: a restart does not lose the watch-list
            for symbol, at in (await self._store.tape_setup_symbols(cutoff)).items():
                self._note_setup(symbol, at)
            self._rebuilt = True
        self._last_setup = {s: at for s, at in self._last_setup.items() if at >= cutoff}
        return set(self._last_setup) | {p.symbol for p in await self._store.active_positions()}

    async def _write_quotes(self, watched: set[str]) -> int:
        if not watched:
            return 0
        books = await self._api.book_tickers()
        at = self._clock()
        # An empty side (a symbol out of trading) or a crossed book is no quote, and the replay
        # refuses the whole tape on one.
        return await self._store.add_tape_quotes(
            at,
            [
                (b.symbol, b.bid_price, b.ask_price)
                for b in books
                if b.symbol in watched and 0 < b.bid_price <= b.ask_price
            ],
        )

    async def _prune(self, now: datetime) -> None:
        today = now.astimezone(UTC).date()
        if today != self._pruned_on:
            pruned = await self._store.prune_tape_quotes(now - self._retention)
            self._pruned_on = today
            log.debug("tape.quotes_pruned", rows=pruned)
