from datetime import timedelta
from decimal import Decimal

from sqlalchemy import insert, update

from tests.support.risk import NOW
from trade_agent.persistence.models import PositionRecord
from trade_agent.persistence.store import Store


async def test_risk_history_never_truncates_at_ui_page_size(store: Store) -> None:
    rows = [
        {
            "profile": "con",
            "decision_id": f"{n:010x}",
            "symbol": "BTCUSDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "state": "closed",
            "entry_mode": "limit_fok",
            "policy": {},
            "planned_qty": Decimal(1),
            "planned_price": Decimal(1),
            "protection_list_id": f"history-{n}",
            "realized_pnl": Decimal(-1),
            "closed_at": NOW - timedelta(seconds=n),
        }
        for n in range(1005)
    ]
    async with store.db.session() as session:
        await session.execute(insert(PositionRecord), rows)
        await session.commit()
    history = await store.risk_history(NOW - timedelta(days=1))
    assert history.consecutive_losses == 1005
    assert history.streaks == {"con": 1005}
    assert history.daily == {"con": Decimal(-1005)}
    async with store.db.session() as session:
        await session.execute(
            update(PositionRecord)
            .where(PositionRecord.decision_id == "0000000000")
            .values(realized_pnl=Decimal(0))
        )
    reset = await store.risk_history(NOW - timedelta(days=1))
    assert reset.consecutive_losses == 0 and reset.streaks == {}
    assert reset.daily == {"con": Decimal(-1004)}
