"""Correct an expired reservation estimate; append an audited adjustment, never delete usage."""

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from trade_agent.config.settings import load_settings
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ResearchStore


async def run(args: argparse.Namespace) -> None:
    settings = load_settings(env_file=args.env_file)
    if settings.database_url is None:
        raise ValueError("TA_DATABASE_URL is required")
    db = Database(settings.database_url.get_secret_value())
    try:
        await ResearchStore(db).adjust_estimate(
            args.token, args.settled_usd, args.reason, datetime.now(UTC)
        )
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--token", required=True, help="Reservation token from the HIGH alert")
    parser.add_argument(
        "--settled-usd",
        type=Decimal,
        required=True,
        help="Verified total replacing this estimate, excluding already recorded usage",
    )
    parser.add_argument(
        "--reason", required=True, help="Provider evidence/reference and explanation"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
