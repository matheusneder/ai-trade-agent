"""Risk Guard: evaluates the stop conditions, applies the actions and validates each order.

The evaluation (``evaluate``) is a pure function over a ``RiskSnapshot``; the application
(``RiskGuard.apply``) persists the new state, records the event and alerts the operator,
once per occurrence of each trigger.
``flatten`` delegates closing the positions to whoever executes orders (the decision engine).
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog

from trade_agent import metrics, tracing
from trade_agent.exchange.models import OrderSide, OrderType
from trade_agent.exchange.rules import SymbolRules
from trade_agent.persistence.store import Severity, Store
from trade_agent.risk.conditions import (
    Action,
    PreTradeConfig,
    StopConditions,
    Trigger,
    in_trading_window,
)
from trade_agent.risk.state import (
    GLOBAL,
    Fired,
    OpState,
    ScopeState,
    StateStore,
    combined,
    escalate,
)
from trade_agent.strategy.portfolio import TradeIdea
from trade_agent.strategy.profiles import ProfileConfig

log = structlog.get_logger(__name__)

PCT = Decimal(100)

type Flattener = Callable[[str], Awaitable[int]]
"""Closes the scope's positions (``global`` or a profile name); returns how many."""


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    now: datetime
    equity: Decimal
    """Agent equity: managed capital + realized PnL + unrealized PnL."""
    day_start_equity: Decimal
    peak_equity: Decimal
    baseline_equity: Decimal
    """Managed capital (the basis of the losses and the target, in %)."""
    consecutive_losses: int = 0
    profile_consecutive_losses: Mapping[str, int] = field(default_factory=dict)
    profile_daily_pnl: Mapping[str, Decimal] = field(default_factory=dict)
    """Realized PnL for the day (UTC) per profile, in the quote asset."""
    profile_capital: Mapping[str, Decimal] = field(default_factory=dict)
    btc_change_1h: float | None = None
    quote_deviation: float | None = None
    """Absolute deviation of the quote asset from the dollar (fraction)."""
    fear_greed: int | None = None
    api_error_rate: float = 0.0
    reconcile_anomalies: int = 0
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    exposure: Decimal = Decimal(0)
    """Cost of the active positions (in the quote asset)."""
    active_positions: int = 0


@dataclass(frozen=True, slots=True)
class Hit:
    scope: str
    condition: str
    action: Action
    value: float
    threshold: float | None
    cooldown: timedelta | None = None
    below: bool = False
    """Fires below the limit (e.g. Fear & Greed)."""

    @property
    def key(self) -> tuple[str, str]:
        return self.scope, self.condition

    @property
    def reason(self) -> str:
        limit = f" (limite {self.threshold:g})" if self.threshold is not None else ""
        return f"{self.condition}: {self.value:.4g}{limit}"

    def worsened(self, previous: float) -> bool:
        """Got one full limit worse since ``previous`` (4 → 8 consecutive losses; daily loss
        of 3% → 6%). No limit (e.g. reconciliation mismatches): any worsening."""
        step = abs(self.threshold or 0)
        down = self.below or (self.threshold or 0) < 0
        if step == 0:
            return self.value < previous if down else self.value > previous
        return self.value <= previous - step if down else self.value >= previous + step


def _pct(numerator: Decimal, denominator: Decimal) -> float:
    return float(numerator / denominator * PCT) if denominator > 0 else 0.0


def _reached(trigger: Trigger, value: float, *, below: bool) -> bool:
    """Value ≥ limit; negative limit: value ≤ limit (drops); ``below``: value < limit;
    no limit (e.g. a reconciliation mismatch): any occurrence."""
    if trigger.value is None:
        return value > 0
    if below:
        return value < trigger.value
    return value <= trigger.value if trigger.value < 0 else value >= trigger.value


def _scaled(value: float | None, factor: float = 100) -> float | None:
    return None if value is None else value * factor


def evaluate(conditions: StopConditions, s: RiskSnapshot) -> list[Hit]:
    """Conditions met in the snapshot (pure function)."""
    g = conditions.global_
    readings: list[tuple[str, str, Trigger | None, float | None, bool]] = [
        (GLOBAL, "max_daily_loss_pct", g.max_daily_loss_pct,
         _pct(s.day_start_equity - s.equity, s.baseline_equity), False),
        (GLOBAL, "max_drawdown_pct", g.max_drawdown_pct,
         _pct(s.peak_equity - s.equity, s.peak_equity), False),
        (GLOBAL, "max_consecutive_losses", g.max_consecutive_losses,
         float(s.consecutive_losses), False),
        (GLOBAL, "btc_move_1h_pct", g.btc_move_1h_pct, _scaled(s.btc_change_1h), False),
        (GLOBAL, "quote_depeg_pct", g.quote_depeg_pct, _scaled(s.quote_deviation), False),
        (GLOBAL, "fear_greed_below", g.fear_greed_below,
         _scaled(None if s.fear_greed is None else float(s.fear_greed), 1), True),
        (GLOBAL, "api_error_rate_5m", g.api_error_rate_5m, s.api_error_rate, False),
        (GLOBAL, "reconcile_mismatch", g.reconcile_mismatch, float(s.reconcile_anomalies), False),
        (GLOBAL, "profit_target_pct", g.profit_target_pct,
         _pct(s.equity - s.baseline_equity, s.baseline_equity), False),
    ]  # fmt: skip
    for name, per in conditions.per_profile.items():
        capital = s.profile_capital.get(name, Decimal(0))
        readings += [
            (name, "max_daily_loss_pct", per.max_daily_loss_pct,
             _pct(-s.profile_daily_pnl.get(name, Decimal(0)), capital), False),
            (name, "max_consecutive_losses", per.max_consecutive_losses,
             float(s.profile_consecutive_losses.get(name, 0)), False),
        ]  # fmt: skip
    return [
        Hit(scope, name, trigger.action, value, trigger.value, trigger.cooldown, below)
        for scope, name, trigger, value, below in readings
        if trigger is not None and value is not None and _reached(trigger, value, below=below)
    ]


# ============================================================================ pre-trade
@dataclass(frozen=True, slots=True)
class PreTradeContext:
    state: OpState
    now: datetime
    active_symbols: frozenset[str] = frozenset()
    vetoed_assets: frozenset[str] = frozenset()
    delisted_symbols: frozenset[str] = frozenset()


def pre_trade_violations(
    idea: TradeIdea,
    *,
    profile: ProfileConfig,
    capital: Decimal,
    rules: SymbolRules,
    context: PreTradeContext,
    conditions: StopConditions,
) -> list[str]:
    """Mandatory checks before any entry (doc 03, §10.1)."""
    config: PreTradeConfig = conditions.pre_trade
    problems: list[str] = []
    if not context.state.allows_entries:
        problems.append(f"estado operacional {context.state.value}")
    if not in_trading_window(conditions.global_.trading_window_utc, context.now):
        problems.append("fora da janela de negociação")
    if idea.symbol in context.active_symbols:
        problems.append("já há posição ativa no ativo")
    if rules.base_asset in context.vetoed_assets:
        problems.append("vetado pelo analista")
    if idea.symbol in context.delisted_symbols:
        problems.append("em delistagem")

    # "always protected": ProtectionPolicy already refuses, when built, a policy without a stop
    policy = idea.policy
    stop_pct = policy.stop_pct or Decimal(policy.stop_trailing_bips or 0) / PCT
    fee = Decimal(str(config.round_trip_fee_pct))
    reward_risk = (policy.take_profit_pct - fee) / (stop_pct + fee)
    if reward_risk < Decimal(str(config.min_reward_risk)):
        problems.append(f"R:R {reward_risk:.2f} abaixo de {config.min_reward_risk}")
    tolerance = 1 + Decimal(str(config.risk_tolerance_pct)) / PCT
    max_risk = capital * profile.allocation.risk_per_trade_pct / PCT * tolerance
    if idea.risk > max_risk:
        problems.append(f"risco {idea.risk:.2f} acima do limite {max_risk:.2f}")
    for bips, order_type in (
        (policy.take_profit_trailing_bips, OrderType.TAKE_PROFIT),
        (policy.stop_trailing_bips, OrderType.STOP_LOSS),
    ):
        if bips is not None:
            problems += rules.trailing_violations(bips, side=OrderSide.SELL, order_type=order_type)
    return problems


# ============================================================================ application
class RiskGuard:
    def __init__(
        self,
        *,
        conditions: StopConditions,
        states: StateStore,
        store: Store,
        notify: Callable[[Severity, str], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.conditions = conditions
        self._states = states
        self._store = store
        self._notify = notify
        self._clock = clock
        self._flatten: Flattener | None = None

    def set_flattener(self, flatten: Flattener) -> None:
        self._flatten = flatten

    async def state(self, scope: str) -> ScopeState:
        return await self._states.get(scope, self._clock())

    async def effective(self, profile: str) -> OpState:
        """The profile's effective state (the most restrictive of global and the profile's)."""
        return combined(await self.state(GLOBAL), await self.state(profile))

    async def _set(self, scope: str, new: ScopeState, *, source: str) -> None:
        tracing.annotate(scope=scope, state=new.state, source=source, reason=new.reason)
        metrics.record_state_change(scope=scope, state=new.state.value, source=source)
        await self._states.put(scope, new)
        severity = Severity.INFO if new.state is OpState.RUNNING else Severity.CRITICAL
        payload = {"scope": scope, "source": source, **new.to_json()}
        await self._store.record_event("risk.state_changed", severity, payload)
        until = f" até {new.until:%Y-%m-%d %H:%M} UTC" if new.until else ""
        await self._notify(
            severity, f"[{scope}] {new.state.value.upper()}{until} — {new.reason or source}"
        )
        log.warning("risk.state_changed", **payload)

    async def fired(self, scope: str) -> list[Fired]:
        """Triggers of the scope that already acted and whose condition still holds."""
        fired = await self._states.fired()
        return [record for (owner, _), record in sorted(fired.items()) if owner == scope]

    @tracing.traced("risk", "risk.apply")
    async def apply(self, hits: Iterable[Hit]) -> list[Hit]:
        """Applies the actions of an evaluation's triggers; returns the ones that changed a state.

        ``hits`` is the whole evaluation: a trigger that is missing no longer holds and is
        rearmed. Each trigger acts once per occurrence: while the condition lasts, it does not
        extend the pause or alert again, and ``/resume`` and the end of the cooldown hold. It
        only acts again if it gets one full limit worse (``Hit.worsened``). A trigger that did
        not change the state (another, more restrictive one was already there) stays armed: it
        acts after a ``/resume``.
        """
        applied: list[Hit] = []
        hits = list(hits)
        tracing.annotate(hits=[h.reason for h in hits])
        log.debug("risk.evaluated", hits=[h.reason for h in hits])
        saved = await self._states.fired()
        active = {hit.key for hit in hits}
        fired = {key: record for key, record in saved.items() if key in active}
        for scope, condition in saved.keys() - fired.keys():
            log.info("risk.trigger_rearmed", scope=scope, condition=condition)
        try:
            for hit in hits:
                previous = fired.get(hit.key)
                if previous is not None and not hit.worsened(previous.value):
                    log.debug("risk.hit_already_fired", scope=hit.scope, reason=hit.reason)
                    continue
                now = self._clock()
                current = await self.state(hit.scope)
                if hit.action is Action.PAUSE:
                    until = now + hit.cooldown if hit.cooldown else None
                    proposed = ScopeState(OpState.PAUSED, hit.reason, now, until)
                elif hit.action is Action.HALT:
                    proposed = ScopeState(OpState.HALTED, hit.reason, now)
                else:
                    proposed = ScopeState(OpState.FLATTENING, hit.reason, now)
                new = escalate(current, proposed)
                if new is None:
                    log.debug("risk.hit_unchanged", scope=hit.scope, state=current.state.value)
                    continue
                applied.append(hit)
                fired[hit.key] = Fired(hit.value, hit.reason, now)
                await self._set(hit.scope, new, source="gatilho")
                if new.state is OpState.FLATTENING:
                    await self._run_flatten(hit.scope, hit.reason)
        finally:
            if fired != saved:
                await self._states.put_fired(fired)
        return applied

    async def _run_flatten(self, scope: str, reason: str) -> int:
        if self._flatten is None:
            raise RuntimeError("flatten sem executor configurado")
        closed = await self._flatten(scope)
        now = self._clock()
        await self._set(
            scope,
            ScopeState(
                OpState.HALTED, f"{reason} (flatten: {closed} posições)", now, flattened=True
            ),
            source="flatten",
        )
        return closed

    # ------------------------------------------------------------------ manual commands
    @tracing.traced("risk", "risk.pause")
    async def pause(self, scope: str, reason: str = "manual") -> None:
        await self._set(scope, ScopeState(OpState.PAUSED, reason, self._clock()), source="manual")

    @tracing.traced("risk", "risk.halt")
    async def halt(self, scope: str, reason: str = "manual") -> None:
        await self._set(scope, ScopeState(OpState.HALTED, reason, self._clock()), source="manual")

    @tracing.traced("risk", "risk.resume")
    async def resume(self, scope: str) -> bool:
        """Returns to ``RUNNING``; refuses during a flatten in progress."""
        if (await self.state(scope)).state is OpState.FLATTENING:
            return False
        await self._set(scope, ScopeState(reason="retomado pelo operador"), source="manual")
        return True

    @tracing.traced("risk", "risk.flatten")
    async def flatten(self, scope: str, reason: str = "manual") -> int:
        now = self._clock()
        await self._set(scope, ScopeState(OpState.FLATTENING, reason, now), source="manual")
        return await self._run_flatten(scope, reason)
