"""Gravação periódica do ``RiskSnapshot``: a verificação de risco roda a cada minuto e
entrega cada snapshot; a telemetria persiste o primeiro (já na partida) e depois um a cada
``interval`` para os dashboards."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select

from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.persistence.db import Database
from trade_agent.persistence.models import TelemetrySnapshotRecord
from trade_agent.risk.guard import RiskGuard, RiskSnapshot
from trade_agent.risk.state import GLOBAL


def drawdown_pct(snapshot: RiskSnapshot) -> float:
    peak = snapshot.peak_equity
    return float((peak - snapshot.equity) / peak * 100) if peak > 0 else 0.0


class TelemetryRecorder:
    def __init__(
        self,
        *,
        db: Database,
        guard: RiskGuard,
        rest: BinanceRestClient,
        scopes: Sequence[str],
        interval: timedelta = timedelta(minutes=5),
    ) -> None:
        self._db = db
        self._guard = guard
        self._rest = rest
        self._scopes = [GLOBAL, *scopes]
        self._interval = interval
        self._recorded_at: datetime | None = None
        self.latest: RiskSnapshot | None = None

    async def observe(self, snapshot: RiskSnapshot) -> bool:
        """Recebe o snapshot da verificação de risco; persiste se a foto estiver vencida."""
        self.latest = snapshot
        if self._recorded_at is not None and snapshot.now - self._recorded_at < self._interval:
            return False
        await self.record()
        self._recorded_at = snapshot.now
        return True

    async def record(self) -> bool:
        """Persiste o último snapshot observado; ``False`` se ainda não houver nenhum."""
        s = self.latest
        if s is None:
            return False
        states = {scope: (await self._guard.state(scope)).state.value for scope in self._scopes}
        profiles = {
            name: {
                "daily_pnl": str(s.profile_daily_pnl.get(name, 0)),
                "losing_streak": s.profile_consecutive_losses.get(name, 0),
            }
            for name in s.profile_capital
        }
        async with self._db.session() as session:
            session.add(
                TelemetrySnapshotRecord(
                    at=s.now,
                    equity=s.equity,
                    realized_pnl=s.realized_pnl,
                    unrealized_pnl=s.unrealized_pnl,
                    day_start_equity=s.day_start_equity,
                    peak_equity=s.peak_equity,
                    drawdown_pct=drawdown_pct(s),
                    exposure=s.exposure,
                    active_positions=s.active_positions,
                    api_error_rate=s.api_error_rate,
                    used_weight_1m=self._rest.usage.used_weight_1m,
                    clock_offset_ms=self._rest.time_offset_ms,
                    btc_change_1h=s.btc_change_1h,
                    quote_deviation=s.quote_deviation,
                    fear_greed=s.fear_greed,
                    states=states,
                    profiles=profiles,
                )
            )
        return True

    async def last_recorded(self) -> TelemetrySnapshotRecord | None:
        async with self._db.session() as session:
            query = select(TelemetrySnapshotRecord).order_by(TelemetrySnapshotRecord.id.desc())
            return (await session.scalars(query.limit(1))).first()
