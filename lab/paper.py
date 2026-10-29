"""Independent TA/LLM portfolios replaying a shared, chronological quote/decision tape.

Input is JSONL: quote {at, symbol, bid, ask}, or decision {at, profile, candidates,
exposure_multiplier}. Candidates serialize the portfolio Candidate dataclass. Each account
owns its cash/positions; no exchange credentials or order APIs are used. Quotes must be
captured independently of the strategy. Sparse quotes cannot validate intrabar fills.

The pair-local families (D-044) replay as the agent and the lab strategy run them: a fixed
(``limit``) target fills at its price once a bid reaches it, the family's technical exit
closes at the decision's bid (the breakout's against the level of its entry signal), and the
profile's ``risk`` pauses new entries (:mod:`trade_agent.risk.scope`), marked to market at
each decision. The global stop conditions are still not replayed.
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from trade_agent.execution.orders import EntryMode, StopMode, TakeProfitMode
from trade_agent.market.candles import INTERVAL_MS
from trade_agent.risk.conditions import StopConditions, load_stop_conditions
from trade_agent.risk.guard import PreTradeContext, pre_trade_violations
from trade_agent.risk.scope import ScopeRisk
from trade_agent.risk.state import OpState
from trade_agent.signals import position_exit
from trade_agent.strategy.exits import DEFAULT_FEE_BUFFER
from trade_agent.strategy.portfolio import Candidate, Holding, TradeIdea, plan_entries
from trade_agent.strategy.profiles import StrategyConfig, load_strategy_config

D = Decimal
CANDIDATES = TypeAdapter(list[Candidate])


@dataclass
class PaperPosition:
    idea: TradeIdea
    quantity: Decimal
    entry: Decimal
    spent: Decimal
    opened_at: datetime
    highest: Decimal
    stop: Decimal
    trailing_active: bool = False
    weak_cycles: int = 0
    activation: Decimal = field(default_factory=lambda: D(0))
    target: Decimal | None = None
    """``limit`` take-profit: the price its limit order sells at."""
    level: float | None = None
    """Pair-local breakout: the level its entry signal broke (technical exit below it)."""


class PaperPortfolio:
    def __init__(
        self,
        strategy: StrategyConfig,
        conditions: StopConditions,
        *,
        pure_ta: bool,
        fee_pct: Decimal,
        slippage_bps: Decimal,
        profiles: Sequence[str] | None = None,
    ) -> None:
        """``profiles``: the ones to simulate, by default the enabled ones (a pair-local
        profile is never enabled: name it, D-044)."""
        if fee_pct < 0 or not 0 <= slippage_bps < 10_000:
            raise ValueError("invalid simulation costs")
        simulated = {n: strategy.profiles[n] for n in profiles or strategy.enabled_profiles()}
        for profile in simulated.values():
            if profile.protection.stop.mode is not StopMode.FIXED:
                raise ValueError("paper replay supports fixed stops only")
        self.strategy, self.conditions = strategy, conditions
        self.pure_ta = pure_ta
        self.fee, self.slippage = fee_pct / 100, slippage_bps / 10_000
        self.cash = {name: strategy.profile_capital(name) for name in simulated}
        self.positions: dict[str, PaperPosition] = {}
        self.books: dict[str, tuple[Decimal, Decimal]] = {}
        self.trades: list[dict[str, Any]] = []
        self.rejections: list[dict[str, str]] = []
        self.scopes: dict[str, ScopeRisk] = {}
        for name, profile in simulated.items():
            if profile.risk is not None:
                candle = timedelta(milliseconds=INTERVAL_MS[profile.timeframe])
                self.scopes[name] = ScopeRisk(
                    profile.risk.limits(strategy.profile_capital(name), candle)
                )

    def close(self, symbol: str, bid: Decimal, at: datetime, reason: str) -> None:
        p = self.positions.pop(symbol)
        # a limit target sells at its own price: no slippage
        slippage = 0 if reason == "target" else self.slippage
        received = p.quantity * bid * (1 - slippage) * (1 - self.fee)
        self.cash[p.idea.profile] += received
        scope = self.scopes.get(p.idea.profile)
        if scope is not None:
            scope.closed(at, symbol, float(received - p.spent), stop=reason == "stop")
        self.trades.append(
            {
                "symbol": symbol,
                "profile": p.idea.profile,
                "at": at.isoformat(),
                "pnl": str(received - p.spent),
                "reason": reason,
            }
        )

    def quote(self, symbol: str, bid: Decimal, ask: Decimal, at: datetime) -> None:
        if not 0 < bid <= ask:
            raise ValueError("invalid quote")
        self.books[symbol] = (bid, ask)
        p = self.positions.get(symbol)
        if p is None:
            return
        if bid <= p.stop:
            self.close(symbol, bid, at, "stop")  # gaps fill at the observed bid, not stop price
            return
        if p.target is not None:
            if bid >= p.target:
                self.close(symbol, p.target, at, "target")
            return
        p.highest = max(p.highest, bid)
        if bid >= p.activation:
            p.trailing_active = True
        delta = D(p.idea.policy.take_profit_trailing_bips or 0) / 10_000
        if p.trailing_active and bid <= p.highest * (1 - delta):
            self.close(symbol, bid, at, "take_profit")

    def decision(
        self, name: str, candidates: list[Candidate], exposure: Decimal, at: datetime
    ) -> None:
        profile = self.strategy.profiles[name]
        if name not in self.cash:
            return
        if self.pure_ta:
            profile = profile.model_copy(
                update={"llm": profile.llm.model_copy(update={"enabled": False})}
            )
        if not profile.llm.enabled:
            exposure = D(1)
        for candidate in candidates:
            self.quote(candidate.member.symbol, candidate.bid, candidate.ask, at)
            self._decision_exit(name, candidate, at)
        for symbol, position in list(self.positions.items()):
            maximum = profile.protection.max_holding
            if position.idea.profile == name and maximum and at - position.opened_at >= maximum:
                self.close(symbol, self.books[symbol][0], at, "max_holding")
        holdings = [
            Holding(p.idea.profile, s, p.idea.tier, p.spent) for s, p in self.positions.items()
        ]
        config = self.conditions.pre_trade
        cost_pct = (
            D(str(config.round_trip_fee_pct + config.slippage_pct))
            if config.risk_model == "cost_adjusted"
            else D(0)
        )
        plan = plan_entries(
            name=name,
            profile=profile,
            capital=self.strategy.profile_capital(name),
            holdings=holdings,
            candidates=candidates,
            exposure_multiplier=exposure,
            execution_cost_pct=cost_pct,
        )
        self.rejections.extend({"symbol": s, "reason": reason} for s, reason in plan.rejections)
        rules = {c.member.symbol: c.member.rules for c in candidates}
        signals = {c.member.symbol: c.signal for c in candidates}
        scope = self.scopes.get(name)
        if scope is not None:
            scope.mark(at, float(self._equity(name)))
        for idea in plan.ideas:
            blocked = scope.blocked(at, idea.symbol) if scope is not None else None
            if blocked is not None:
                self.rejections.append({"symbol": idea.symbol, "reason": blocked})
                continue
            context = PreTradeContext(
                state=OpState.RUNNING,
                now=at,
                active_symbols=frozenset(self.positions),
                account_capital=self.strategy.account.managed_capital,
                exposure=sum((p.spent for p in self.positions.values()), D(0)),
                open_risk=sum((p.idea.risk for p in self.positions.values()), D(0)),
            )
            problems = pre_trade_violations(
                idea,
                profile=profile,
                capital=self.strategy.profile_capital(name),
                rules=rules[idea.symbol],
                context=context,
                conditions=self.conditions,
            )
            bid, ask = self.books[idea.symbol]
            price = ask * (1 + self.slippage)
            spent = idea.entry.quantity * price * (1 + self.fee)
            if (
                problems
                or idea.entry.mode is not EntryMode.LIMIT_FOK
                or price > idea.entry.limit_price
                or spent > self.cash[name]
            ):
                self.rejections.append(
                    {"symbol": idea.symbol, "reason": "; ".join(problems) or "FOK/cash unavailable"}
                )
                continue
            self.cash[name] -= spent
            limit = idea.policy.take_profit_mode is TakeProfitMode.LIMIT
            self.positions[idea.symbol] = PaperPosition(
                idea,
                idea.entry.quantity,
                price,
                spent,
                at,
                bid,
                price * (1 - (idea.policy.stop_pct or D(0)) / 100),
                activation=price * (1 + idea.policy.take_profit_pct / 100),
                target=price * (1 + idea.policy.take_profit_pct / 100) if limit else None,
                level=signals[idea.symbol].level,
            )

    def _decision_exit(self, name: str, candidate: Candidate, at: datetime) -> None:
        symbol = candidate.member.symbol
        p = self.positions.get(symbol)
        if p is None or p.idea.profile != name:
            return
        profile = self.strategy.profiles[name]
        if profile.family.local:  # the family's technical exit (D-044)
            reason = position_exit(candidate.signal, profile.local_params(), p.level)
            expired = (
                profile.protection.max_holding is not None
                and at - p.opened_at >= profile.protection.max_holding
            )
            if reason is not None or expired:
                self.close(symbol, candidate.bid, at, "decision")
            return
        p.weak_cycles = (
            p.weak_cycles + 1 if candidate.signal.score <= profile.exits.exit_score else 0
        )
        veto = (
            not self.pure_ta
            and profile.llm.enabled
            and candidate.opinion is not None
            and candidate.opinion.veto
        )
        expired = (
            profile.protection.max_holding is not None
            and at - p.opened_at >= profile.protection.max_holding
        )
        if veto or expired or p.weak_cycles >= profile.exits.exit_after_cycles:
            self.close(symbol, candidate.bid, at, "decision")
        elif profile.protection.break_even_after_r is not None:
            trigger = (
                D(str(profile.protection.break_even_after_r))
                * (p.idea.policy.stop_pct or D(0))
                / 100
            )
            if candidate.bid >= p.entry * (1 + trigger):
                new_stop = p.entry * (1 + DEFAULT_FEE_BUFFER)
                if p.stop < new_stop < candidate.bid:
                    p.stop = new_stop
                    # Production replaces the native OCO, resetting its trailing high.
                    p.highest = candidate.bid
                    p.trailing_active = False
                    if p.activation <= candidate.bid:
                        p.activation = candidate.bid * (1 + DEFAULT_FEE_BUFFER)

    def _equity(self, name: str) -> Decimal:
        """The profile's cash and its positions at the last bid, after fees."""
        held = sum(
            (
                p.quantity * self.books[s][0] * (1 - self.fee)
                for s, p in self.positions.items()
                if p.idea.profile == name
            ),
            D(0),
        )
        return self.cash[name] + held

    def summary(self) -> dict[str, Any]:
        marked = sum(
            (
                p.quantity * self.books[s][0] * (1 - self.fee) * (1 - self.slippage)
                for s, p in self.positions.items()
            ),
            D(0),
        )
        initial = sum((self.strategy.profile_capital(n) for n in self.cash), D(0))
        return {
            "cash": self.cash,
            "open_positions": len(self.positions),
            "trades": self.trades,
            "rejections": self.rejections,
            "pnl": sum(self.cash.values(), D(0)) + marked - initial,
        }


def replay(path: Path, accounts: dict[str, PaperPortfolio]) -> dict[str, Any]:
    previous: datetime | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        at = datetime.fromisoformat(event["at"])
        if at.tzinfo is None or (previous is not None and at < previous):
            raise ValueError("tape must use chronological, timezone-aware timestamps")
        previous = at
        for account in accounts.values():
            if event["kind"] == "quote":
                account.quote(event["symbol"], D(event["bid"]), D(event["ask"]), at)
            elif event["kind"] == "decision":
                account.decision(
                    event["profile"],
                    CANDIDATES.validate_python(event["candidates"]),
                    D(event["exposure_multiplier"]),
                    at,
                )
            else:
                raise ValueError("unknown tape event")
    return {name: account.summary() for name, account in accounts.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tape", type=Path)
    parser.add_argument("--profiles", type=Path, default=Path("config/profiles.yaml"))
    parser.add_argument("--risk", type=Path, default=Path("config/stop_conditions.yaml"))
    parser.add_argument("--fee-pct", type=Decimal, default=D("0.1"))
    parser.add_argument("--slippage-bps", type=Decimal, required=True)
    args = parser.parse_args()
    strategy, conditions = load_strategy_config(args.profiles), load_stop_conditions(args.risk)
    accounts = {
        name: PaperPortfolio(
            strategy, conditions, pure_ta=pure, fee_pct=args.fee_pct, slippage_bps=args.slippage_bps
        )
        for name, pure in (("pure_ta", True), ("ta_llm", False))
    }
    print(
        json.dumps(
            {
                "results": replay(args.tape, accounts),
                "limitations": [
                    "quote-driven fills; no queue or depth model",
                    "no full global circuit-breaker simulation",
                    "LLM and infrastructure costs must be subtracted separately",
                    "not profitability approval",
                ],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
