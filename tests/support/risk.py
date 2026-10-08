"""Support for the risk tests: calm snapshot, trigger table and closed positions."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trade_agent.execution.orders import EntryMode, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.execution.positions import Position, PositionState
from trade_agent.persistence.store import Store
from trade_agent.risk.conditions import Action, ProfileConditions, Trigger, load_stop_conditions
from trade_agent.risk.guard import RiskSnapshot
from trade_agent.risk.state import GLOBAL

D = Decimal
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
STOP_CONDITIONS_FILE = Path(__file__).parents[2] / "config" / "stop_conditions.yaml"
# The repository's conditions, plus a per-profile limit for "moderado" in the test profiles
# (tests/fixtures/profiles.yaml): the repository has no per-profile limit.
DAILY_LOSS_2PCT = Trigger(value=2, action=Action.PAUSE, cooldown=timedelta(hours=24))
CONDITIONS = load_stop_conditions(STOP_CONDITIONS_FILE).model_copy(
    update={"per_profile": {"moderado": ProfileConditions(max_daily_loss_pct=DAILY_LOSS_2PCT)}}
)
POLICY = ProtectionPolicy(
    TakeProfitMode.TRAILING, D("10.8"), StopMode.FIXED, take_profit_trailing_bips=130, stop_pct=D(4)
)


def snapshot(**overrides: Any) -> RiskSnapshot:
    calm = RiskSnapshot(
        now=NOW,
        equity=D(1000),
        day_start_equity=D(1000),
        peak_equity=D(1000),
        baseline_equity=D(1000),
        profile_capital={"conservador": D(500), "moderado": D(300)},
        btc_change_1h=0.0,
        quote_deviation=0.0,
        fear_greed=50,
    )
    return replace(calm, **overrides)


# Each trigger of config/stop_conditions.yaml: forced condition → expected action.
TRIGGERS: list[tuple[str, str, dict[str, Any], Action]] = [
    ("max_daily_loss_pct", GLOBAL, {"equity": D(969), "day_start_equity": D(1000)}, Action.PAUSE),
    ("max_drawdown_pct", GLOBAL,
     {"equity": D(1015), "day_start_equity": D(1015), "peak_equity": D(1200)}, Action.HALT),
    ("max_consecutive_losses", GLOBAL, {"consecutive_losses": 4}, Action.PAUSE),
    ("btc_move_1h_pct", GLOBAL, {"btc_change_1h": -0.061}, Action.PAUSE),
    ("quote_depeg_pct", GLOBAL, {"quote_deviation": 0.016}, Action.FLATTEN),
    ("fear_greed_below", GLOBAL, {"fear_greed": 9}, Action.PAUSE),
    ("api_error_rate_5m", GLOBAL, {"api_error_rate": 0.25}, Action.PAUSE),
    ("reconcile_mismatch", GLOBAL, {"reconcile_anomalies": 1}, Action.PAUSE),
    ("max_daily_loss_pct", "moderado", {"profile_daily_pnl": {"moderado": D("-6.1")}},
     Action.PAUSE),
]  # fmt: skip

_decisions = itertools.count(1)


async def closed_position(
    store: Store,
    *,
    profile: str,
    pnl: str,
    closed_at: datetime,
    symbol: str = "BTCUSDT",
) -> Position:
    decision = f"{next(_decisions):010x}"
    position, _ = await store.create_position(
        profile=profile,
        decision_id=decision,
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        entry_mode=EntryMode.LIMIT_FOK,
        policy=POLICY,
        planned_qty=D("0.001"),
        planned_price=D(63000),
        protection_list_id=f"ta1-{profile}-{decision}-0-L",
        intent_endpoint="opoco",
        intent_payload={},
    )
    return await store.update_position(
        position.id, state=PositionState.CLOSED, realized_pnl=D(pnl), closed_at=closed_at
    )
