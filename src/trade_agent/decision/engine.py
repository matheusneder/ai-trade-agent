"""Per-profile decision engine, run at the close of the profile's candle.

1. universe (built every cycle) and **closed** candles of the assets in the profile's tiers;
2. technical signals (the same code as the lab);
3. LLM analyst, only when there are setups or positions (saves cost), with per-profile
   degradation;
4. rule-based exits for the profile's positions (rotation, time, veto) and break-even;
5. entries: portfolio (``plan_entries``) → pre-trade checks → OPOCO.

With ``dry_run`` (the ``TA_TRADING_ENABLED`` lock off), decisions are recorded as events
and no order is sent.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import structlog

from trade_agent import metrics, tracing
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.execution.positions import ExitReason, Position, PositionState
from trade_agent.execution.service import PositionService
from trade_agent.market.candles import fetch_candles
from trade_agent.market.universe import (
    Tier,
    Universe,
    UniverseConfig,
    UniverseMember,
    build_universe,
)
from trade_agent.persistence.store import Severity, Store
from trade_agent.research.models import CandidateContext
from trade_agent.research.reading import MarketReading, market_reading
from trade_agent.research.service import ResearchService
from trade_agent.risk.guard import PreTradeContext, RiskGuard, pre_trade_violations
from trade_agent.risk.state import GLOBAL, OpState
from trade_agent.signals import FeatureParams, Signal, compute_features, evaluate
from trade_agent.strategy.exits import break_even_protection, exit_reason, next_weak_cycles
from trade_agent.strategy.portfolio import Candidate, Holding, plan_entries
from trade_agent.strategy.profiles import ProfileConfig, StrategyConfig

log = structlog.get_logger(__name__)

BENCHMARK = "BTCUSDT"
RESEARCH_SLOTS = 8
"""Maximum number of candidates with a setup sent to the analyst per cycle (plus positions)."""
_EXIT_STATES = {PositionState.PROTECTED}
_NO_FILL_STATES = {PositionState.PLANNED, PositionState.ENTRY_SENT}


UNIVERSE_TTL = timedelta(minutes=5)
"""Shorter than the profiles' shortest timeframe (15m): every cycle builds the universe again."""


class UniverseCache:
    """Universe of each cycle, with the volume ranking of the moment.

    An asset breaking out on strong volume climbs the ranking precisely during the breakout
    hours. With a 6 h cache and 4 h cycles, every other cycle used the previous cycle's
    universe and left those assets out (ONE and AR on 2026-10-03). The cache only lets the
    profiles that close a candle together share one build (the lock prevents two).
    """

    def __init__(
        self,
        api: BinanceSpotApi,
        config: UniverseConfig,
        *,
        ttl: timedelta = UNIVERSE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._config = config
        self._ttl_s = ttl.total_seconds()
        self._clock = clock
        self._cached: tuple[float, Universe] | None = None
        self._lock = asyncio.Lock()

    @tracing.traced("decision", "universe.get")
    async def get(self) -> Universe:
        async with self._lock:
            if self._cached is None or self._clock() - self._cached[0] >= self._ttl_s:
                self._cached = (self._clock(), await build_universe(self._api, self._config))
            return self._cached[1]


@dataclass(frozen=True, slots=True)
class CycleReport:
    profile: str
    at: datetime
    state: OpState
    evaluated: int
    dry_run: bool
    research: str
    opened: tuple[str, ...] = ()
    exits: tuple[tuple[str, str], ...] = ()
    rejected: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "state": self.state.value,
            "evaluated": self.evaluated,
            "dry_run": self.dry_run,
            "research": self.research,
            "opened": list(self.opened),
            "exits": [list(e) for e in self.exits],
            "rejected": [list(r) for r in self.rejected],
        }


class DecisionEngine:
    def __init__(
        self,
        *,
        api: BinanceSpotApi,
        positions: PositionService,
        store: Store,
        guard: RiskGuard,
        strategy: StrategyConfig,
        universe: UniverseCache,
        research: ResearchService | None = None,
        view_max_age: timedelta = timedelta(hours=8),
        dry_run: bool = True,
        notify: Callable[[Severity, str], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api = api
        self._positions = positions
        self._store = store
        self._guard = guard
        self._strategy = strategy
        self._universe = universe
        self._research = research
        self._view_max_age = view_max_age
        self._dry_run = dry_run
        self._notify = notify
        self._clock = clock
        self._names = {p.code: name for name, p in strategy.profiles.items()}
        self._features = FeatureParams()
        self._entry_lock = asyncio.Lock()

    # ------------------------------------------------------------------ signals
    async def _signals(
        self, members: list[UniverseMember], profile: ProfileConfig, now_ms: int
    ) -> dict[str, Signal]:
        limit = self._features.warmup + 50
        frames = {
            BENCHMARK: await fetch_candles(
                self._api, BENCHMARK, profile.timeframe, limit=limit, now_ms=now_ms
            )
        }
        benchmark = frames[BENCHMARK]["close"]
        params = profile.signal_params()
        signals: dict[str, Signal] = {}
        for member in members:
            frame = frames.get(member.symbol)
            if frame is None:
                frame = await fetch_candles(
                    self._api, member.symbol, profile.timeframe, limit=limit, now_ms=now_ms
                )
            if len(frame) < self._features.warmup:
                continue  # not enough history for the indicators
            reference = None if member.symbol == BENCHMARK else benchmark
            features = compute_features(frame, self._features, benchmark_close=reference)
            signal = evaluate(features, params)
            signals[member.symbol] = signal
            log.debug(
                "decision.signal",
                profile=profile.code,
                symbol=member.symbol,
                tier=member.tier.value,
                score=round(signal.score, 3),
                setup=signal.setup,
                stop_pct=round(signal.stop_pct, 4),
                close=signal.close,
            )
        return signals

    # ------------------------------------------------------------------ analyst
    async def _reading(
        self,
        name: str,
        profile: ProfileConfig,
        universe: Universe,
        signals: dict[str, Signal],
        own: list[Position],
    ) -> tuple[MarketReading, str]:
        now = self._clock()
        if self._research is None:
            return market_reading(
                None, profile.llm, now=now, max_age=self._view_max_age
            ), "sem analista"
        by_symbol = universe.by_symbol()
        setups = sorted(
            (s for s, sig in signals.items() if sig.setup is not None),
            key=lambda s: signals[s].score,
            reverse=True,
        )[:RESEARCH_SLOTS]
        held = {p.symbol for p in own}
        contexts = [
            CandidateContext(
                asset=by_symbol[s].base_asset,
                symbol=s,
                tier=by_symbol[s].tier.value,
                ta_score=signals[s].score if s in signals else 0.0,
                setup=signals[s].setup if s in signals else None,
                held=s in held,
            )
            for s in dict.fromkeys([*setups, *held])
            if s in by_symbol
        ]
        status = "sem candidatos (leitura anterior)"
        view = None
        if contexts:
            result = await self._research.run_cycle(trigger=f"ciclo:{name}", candidates=contexts)
            view = result.view
            status = "ok" if result.ok else f"falhou: {result.error}"
        if view is None:
            view = await self._research.latest_view()
        return market_reading(view, profile.llm, now=now, max_age=self._view_max_age), status

    # ------------------------------------------------------------------ cycle
    @tracing.traced("decision", "decision.cycle")
    async def run_profile(self, name: str) -> CycleReport:
        profile = self._strategy.profiles[name]
        state = await self._guard.effective(name)
        universe = await self._universe.get()
        members = [m for m in universe.members if profile.tier_limit(m.tier) > 0]
        if not members:
            reasons = Counter(universe.excluded.values()).most_common(3)
            log.warning("decision.empty_universe", profile=name, excluded=dict(reasons))
        log.debug(
            "decision.cycle_start",
            profile=name,
            state=state.value,
            universe=len(universe.members),
            eligible=len(members),
            dry_run=self._dry_run,
        )
        signals = await self._signals(members, profile, self._api.rest.now_ms())

        active = await self._store.active_positions()
        own = [p for p in active if p.profile == profile.code]
        reading, research_status = await self._reading(name, profile, universe, signals, own)
        log.debug(
            "decision.reading",
            profile=name,
            research=research_status,
            degraded=reading.degraded,
            reason=reading.reason,
            exposure=str(reading.exposure_multiplier),
            vetoed=sorted(a for a, o in reading.opinions.items() if o.veto),
        )

        exits = await self._exits(profile, own, signals, reading, state)
        opened: list[str] = []
        rejected: list[tuple[str, str]] = []
        if state.allows_entries:
            # Profiles of the same timeframe run together, and the research (minutes) separates
            # the reading at the start of the cycle from the purchase: one profile buys at a time,
            # with the positions read again, or both buy the same asset (one_position_per_asset).
            async with self._entry_lock:
                opened, rejected = await self._entries(
                    name,
                    profile=profile,
                    universe=universe,
                    signals=signals,
                    active=await self._store.active_positions(),
                    reading=reading,
                    state=state,
                )
        report = CycleReport(
            profile=name,
            at=self._clock(),
            state=state,
            evaluated=len(signals),
            dry_run=self._dry_run,
            research=research_status,
            opened=tuple(opened),
            exits=tuple(exits),
            rejected=tuple(rejected),
        )
        await self._store.record_event("decision.cycle", Severity.INFO, report.summary())
        tracing.annotate(
            profile=name,
            state=state,
            dry_run=self._dry_run,
            evaluated=len(signals),
            research=research_status,
            opened=opened,
            exits=len(exits),
            rejected=len(rejected),
        )
        metrics.record_cycle(
            profile=name,
            state=state.value,
            dry_run=self._dry_run,
            opened=len(opened),
            exits=len(exits),
        )
        log.info("decision.cycle", **report.summary())
        if (opened or exits) and self._notify is not None:
            prefix = "[simulação] " if self._dry_run else ""
            lines = [f"{prefix}{name}: entradas {opened or '—'}; saídas {exits or '—'}"]
            await self._notify(Severity.INFO, "\n".join(lines))
        return report

    async def _exits(
        self,
        profile: ProfileConfig,
        own: list[Position],
        signals: dict[str, Signal],
        reading: MarketReading,
        state: OpState,
    ) -> list[tuple[str, str]]:
        exits: list[tuple[str, str]] = []
        now = self._clock()
        for position in own:
            if position.state not in _EXIT_STATES:
                continue
            key = f"exit.weak.{position.decision_id}"
            previous = int((await self._store.get_checkpoint(key) or {}).get("cycles", 0))
            weak = next_weak_cycles(previous, signals.get(position.symbol), profile)
            await self._store.set_checkpoint(key, {"cycles": weak})
            opinion = reading.opinions.get(position.base_asset)
            age = now - position.opened_at if position.opened_at else None
            reason = exit_reason(
                profile=profile, age=age, weak_cycles=weak, veto=bool(opinion and opinion.veto)
            )
            log.debug(
                "decision.exit_check",
                position_id=position.id,
                symbol=position.symbol,
                weak_cycles=weak,
                age_h=round(age.total_seconds() / 3600, 1) if age else None,
                reason=reason,
                rule_exits_allowed=state.allows_rule_exits,
            )
            if not state.allows_rule_exits:
                continue
            if reason is not None:
                exits.append((position.symbol, reason))
                if not self._dry_run:
                    await self._positions.close_position(position, ExitReason.DECISION)
                continue
            bid = (await self._api.book_ticker(position.symbol)).bid_price
            protection = break_even_protection(position, profile, bid)
            if protection is not None and not self._dry_run:
                await self._positions.adjust_protection(position, protection)
        return exits

    async def _entries(
        self,
        name: str,
        *,
        profile: ProfileConfig,
        universe: Universe,
        signals: dict[str, Signal],
        active: list[Position],
        reading: MarketReading,
        state: OpState,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        by_symbol = universe.by_symbol()
        books = {b.symbol: b for b in await self._api.book_tickers() if b.symbol in signals}
        candidates = [
            Candidate(
                member=by_symbol[symbol],
                signal=signal,
                bid=books[symbol].bid_price,
                ask=books[symbol].ask_price,
                opinion=reading.opinions.get(by_symbol[symbol].base_asset),
            )
            for symbol, signal in signals.items()
            if symbol in books
        ]
        holdings = [
            Holding(
                profile=self._names.get(p.profile, p.profile),
                symbol=p.symbol,
                tier=by_symbol[p.symbol].tier if p.symbol in by_symbol else Tier.SMALL,
                cost=p.entry_quote or p.planned_qty * p.planned_price,
            )
            for p in active
        ]
        capital = self._strategy.profile_capital(name)
        plan = plan_entries(
            name=name,
            profile=profile,
            capital=capital,
            holdings=holdings,
            candidates=candidates,
            exposure_multiplier=reading.exposure_multiplier,
            one_position_per_asset=self._strategy.account.one_position_per_asset,
        )
        log.debug(
            "decision.plan",
            profile=name,
            candidates=len(candidates),
            ideas=[(i.symbol, str(i.notional), str(i.risk)) for i in plan.ideas],
            rejections=[r for r in plan.rejections if r[1] != "sem setup"],
            exposure=str(reading.exposure_multiplier),
        )
        context = PreTradeContext(
            state=state,
            now=self._clock(),
            active_symbols=frozenset(p.symbol for p in active),
            vetoed_assets=frozenset(a for a, o in reading.opinions.items() if o.veto),
        )
        opened: list[str] = []
        rejected = [r for r in plan.rejections if r[1] != "sem setup"]
        for idea in plan.ideas:
            problems = pre_trade_violations(
                idea,
                profile=profile,
                capital=capital,
                rules=by_symbol[idea.symbol].rules,
                context=context,
                conditions=self._guard.conditions,
            )
            if problems:
                log.debug("decision.pre_trade_rejected", symbol=idea.symbol, problems=problems)
                rejected.append((idea.symbol, "; ".join(problems)))
                continue
            if self._dry_run:
                await self._store.record_event(
                    "decision.entry_simulated",
                    Severity.INFO,
                    {"profile": name, "symbol": idea.symbol, "setup": idea.setup,
                     "score": str(idea.score), "notional": str(idea.notional),
                     "risk": str(idea.risk)},
                )  # fmt: skip
            else:
                await self._positions.open_position(
                    profile=profile.code, entry=idea.entry, policy=idea.policy
                )
            opened.append(idea.symbol)
            context = replace(context, active_symbols=context.active_symbols | {idea.symbol})
        return opened, rejected

    # ------------------------------------------------------------------ flatten
    @tracing.traced("decision", "decision.flatten")
    async def flatten(self, scope: str) -> int:
        """Closes the scope's positions: sells the ones with a fill and cancels the pending
        entries. Returns how many were handled."""
        active = await self._store.active_positions()
        targets = [p for p in active if scope == GLOBAL or self._names.get(p.profile) == scope]
        handled = 0
        for position in targets:
            try:
                if position.state in _NO_FILL_STATES:
                    await self._positions.gateway.cancel_order_list(
                        position.symbol, position.protection_list_id
                    )
                    await self._positions.sync(position)
                else:
                    await self._positions.close_position(position, ExitReason.RISK)
                handled += 1
            except Exception as exc:  # one failure does not stop the others
                log.error("flatten.failed", position=position.id, error=repr(exc))
                await self._store.record_event(
                    "flatten.failed", Severity.CRITICAL, {"error": repr(exc)},
                    position_id=position.id,
                )  # fmt: skip
        return handled
