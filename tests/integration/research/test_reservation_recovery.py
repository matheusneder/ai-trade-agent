import argparse
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from scripts import reconcile_research
from sqlalchemy import select

from tests.support.claude import NOW
from trade_agent.persistence.db import Database
from trade_agent.persistence.models import CheckpointRecord, LlmUsageRecord
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.research.llm import BudgetExceededError


async def test_expiry_is_atomic_auditable_and_recovers_across_midnight(db: Database) -> None:
    store = ResearchStore(db)
    day = NOW.replace(hour=0)
    key, token = await store.reserve_budget(day, Decimal(1), Decimal(1), now=NOW)
    assert await store.expire_budget(NOW + timedelta(hours=24, microseconds=-1)) == []
    with pytest.raises(BudgetExceededError):
        await store.reserve_budget(
            day + timedelta(days=1), Decimal(1), Decimal(1), now=NOW + timedelta(hours=23)
        )
    # Fresh store objects represent independent/restarted workers against the durable DB.
    results = await asyncio.gather(
        *[ResearchStore(db).expire_budget(NOW + timedelta(hours=24)) for _ in range(5)]
    )
    assert [token for result in results for token in result] == [token]
    assert await store.spent_since(day, until=day + timedelta(days=1)) == 1
    assert await store.spent_since(day + timedelta(days=1)) == 0
    async with db.session() as session:
        rows = list(await session.scalars(select(LlmUsageRecord)))
        assert len(rows) == 1
        assert (rows[0].purpose, rows[0].model, rows[0].at) == (
            "reservation_expired",
            f"reservation:{token}",
            day,
        )
        checkpoint = await session.get(CheckpointRecord, key)
        assert checkpoint is not None and checkpoint.value == {}
    # Expiry does not consume every future day's budget; concurrent admissions remain atomic.
    outcomes = await asyncio.gather(
        *[
            ResearchStore(db).reserve_budget(
                day + timedelta(days=1), Decimal(1), Decimal(1), now=NOW + timedelta(hours=24)
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, BudgetExceededError) for result in outcomes) == 1


async def test_legacy_reservation_gets_conservative_ttl_and_adjustment(db: Database) -> None:
    day = NOW.replace(hour=0)
    token = "legacy"
    async with db.session() as session:
        session.add(CheckpointRecord(key=f"research.budget.{day.date()}", value={token: "1"}))
    store = ResearchStore(db)
    assert await store.expire_budget(day + timedelta(hours=47)) == []
    assert await store.expire_budget(day + timedelta(hours=48)) == [token]
    with pytest.raises(ValueError, match="unknown"):
        await store.adjust_estimate("missing", Decimal(0), "invoice", NOW)
    for value, reason in [(Decimal(-1), "invoice"), (Decimal("NaN"), "invoice"), (Decimal(0), " ")]:
        with pytest.raises(ValueError, match="finite"):
            await store.adjust_estimate(token, value, reason, NOW)
    await asyncio.gather(
        *[
            ResearchStore(db).adjust_estimate(token, Decimal(".25"), "provider invoice #123", NOW)
            for _ in range(3)
        ]
    )
    assert await store.spent_since(day, until=day + timedelta(days=1)) == Decimal(".25")
    await store.adjust_estimate(token, Decimal(".5"), "corrected invoice #124", NOW)
    async with db.session() as session:
        rows = list(await session.scalars(select(LlmUsageRecord).order_by(LlmUsageRecord.id)))
        assert [r.cost_usd for r in rows] == [Decimal(1), Decimal("-.75"), Decimal(".25")]
        audit = await session.get(CheckpointRecord, f"research.settlement.{token}")
        assert audit is not None and len(audit.value["adjustments"]) == 2
        assert audit.value["adjustments"][0]["reason"] == "provider invoice #123"


async def test_reconciliation_cli_records_adjustment(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResearchStore(db)
    day = NOW.replace(hour=0)
    _, token = await store.reserve_budget(day, Decimal(1), Decimal(1), now=NOW)
    await store.expire_budget(NOW + timedelta(hours=24))
    monkeypatch.setattr(
        reconcile_research,
        "load_settings",
        lambda **_: SimpleNamespace(database_url=SecretStr("test")),
    )
    monkeypatch.setattr(reconcile_research, "Database", lambda _: db)
    await reconcile_research.run(
        argparse.Namespace(
            env_file="unused", token=token, settled_usd=Decimal(".4"), reason="invoice"
        )
    )
    assert await store.spent_since(day) == Decimal(".4")
    monkeypatch.setattr(
        reconcile_research, "load_settings", lambda **_: SimpleNamespace(database_url=None)
    )
    with pytest.raises(ValueError, match="TA_DATABASE_URL"):
        await reconcile_research.run(argparse.Namespace(env_file="unused"))
