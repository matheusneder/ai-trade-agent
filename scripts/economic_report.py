"""Read-only economic PnL report with explicit USD/quote conversion and expense input."""

import argparse
import asyncio
import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from trade_agent.config.settings import load_settings
from trade_agent.economics import OperatingCost, economic_pnl
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.persistence.store import Store
from trade_agent.strategy.profiles import load_strategy_config


async def report(
    db: Database,
    *,
    since: datetime,
    until: datetime,
    costs: list[OperatingCost],
    usd_in_quote: Decimal,
) -> dict[str, Any]:
    store = Store(db)
    positions = [
        p
        for p in await store.closed_positions(limit=None, since=since)
        if p.closed_at is not None and p.closed_at < until
    ]
    unresolved: list[int] = []
    for position in positions:
        valuation = await store.get_checkpoint(f"position.fee_valuation.{position.id}")
        third_asset = any(
            a not in {position.base_asset, position.quote_asset} and Decimal(q) > 0
            for a, q in position.fees.items()
        )
        if third_asset and (valuation is None or valuation.get("missing_fill_ids")):
            unresolved.append(position.id)
    pnl = economic_pnl(
        sum((p.realized_pnl or Decimal(0) for p in positions), Decimal(0)),
        await ResearchStore(db).spent_since(since, until=until),
        costs,
        since=since,
        until=until,
        usd_in_quote=usd_in_quote,
    )
    return {
        "since": since.isoformat(),
        "until_exclusive": until.isoformat(),
        "usd_in_quote": str(usd_in_quote),
        "closed_positions": len(positions),
        "unpriced_commission_positions": unresolved,
        "complete_commission_valuation": not unresolved,
        "expenses": [c.model_dump(mode="json") for c in costs if since <= c.at < until],
        **dataclasses.asdict(pnl),
    }


async def run(args: argparse.Namespace) -> None:
    settings = load_settings(env_file=args.env_file)
    if settings.database_url is None:
        raise ValueError("TA_DATABASE_URL is required")
    costs = TypeAdapter(list[OperatingCost]).validate_json(args.costs.read_bytes())
    db = Database(settings.database_url.get_secret_value())
    try:
        result = await report(
            db, since=args.since, until=args.until, costs=costs, usd_in_quote=args.usd_in_quote
        )
        result["quote_asset"] = load_strategy_config(settings.strategy_config).account.quote_asset
        print(json.dumps(result, indent=2, default=str))
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--usd-in-quote", type=Decimal, required=True)
    parser.add_argument(
        "--costs",
        type=Path,
        required=True,
        help="JSON expense ledger; [] explicitly declares no entered expenses",
    )
    args = parser.parse_args()
    if args.since.tzinfo is None or args.until.tzinfo is None:
        parser.error("timestamps must include UTC offsets")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
