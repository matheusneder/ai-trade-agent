from datetime import timedelta
from decimal import Decimal

from scripts.economic_report import report

from tests.support.risk import NOW, closed_position
from trade_agent.economics import OperatingCost
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Store


async def test_report_flags_unvalued_legacy_fees_and_subtracts_expenses(
    db: Database, store: Store
) -> None:
    position = await closed_position(store, profile="con", pnl="10", closed_at=NOW)
    await store.update_position(position.id, fees={"BNB": "0.01"})
    await closed_position(store, profile="con", pnl="99", closed_at=NOW + timedelta(days=1))
    result = await report(
        db,
        since=NOW,
        until=NOW + timedelta(days=1),
        usd_in_quote=Decimal(1),
        costs=[OperatingCost(at=NOW, amount_usd=Decimal(3), description="hosting")],
    )
    assert result["closed_positions"] == 1
    assert result["economic_pnl_quote"] == Decimal(7)
    assert result["unpriced_commission_positions"] == [position.id]
    assert result["complete_commission_valuation"] is False
