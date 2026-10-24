"""Read-only audit: replays the closed Demo positions' exits on the production tape.

Prints, per position, the Demo exit and the exit the same protection legs would have had on
``api.binance.com`` (public market data, no keys), with the Demo PnL repriced at the
production fill. Model and limits: ``trade_agent.demo_audit``.
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta

import structlog

from trade_agent.config.settings import load_settings
from trade_agent.demo_audit import BinanceTape, PositionAudit, audit_position, render, summary
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.environments import BinanceEnvironment, endpoints_for
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.log import configure_logging
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Store


async def run(args: argparse.Namespace) -> None:
    # the report goes to stdout: warnings and errors only, on stderr
    configure_logging("WARNING")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    settings = load_settings(env_file=args.env_file)
    if settings.database_url is None:
        raise ValueError("TA_DATABASE_URL is required")
    if settings.binance_env is BinanceEnvironment.PROD:
        raise ValueError("the audit compares Demo exits with production; TA_BINANCE_ENV is prod")
    db = Database(settings.database_url.get_secret_value())
    demo_rest = BinanceRestClient(endpoints_for(settings.binance_env).rest)
    real_rest = BinanceRestClient(endpoints_for(BinanceEnvironment.PROD).rest)
    try:
        store = Store(db)
        positions = [
            p
            for p in await store.closed_positions(limit=None, since=args.since)
            if p.closed_at is not None and p.closed_at < args.until
        ]
        demo_tape = BinanceTape(BinanceSpotApi(demo_rest))
        real_tape = BinanceTape(BinanceSpotApi(real_rest))
        audits: list[PositionAudit] = []
        for position in sorted(positions, key=lambda p: p.id):
            audit = await audit_position(
                store,
                position,
                demo_tape=demo_tape,
                real_tape=real_tape,
                horizon_ms=int(args.horizon.total_seconds() * 1000),
                now_ms=int(time.time() * 1000),
            )
            if audit is not None:
                audits.append(audit)
        if args.json:
            rows = [asdict(a) | {"real_pnl": a.real_pnl, "confirmed": a.confirmed} for a in audits]
            print(json.dumps({"positions": rows, "summary": summary(audits)}, default=str))
        else:
            print(render(audits))
    finally:
        await demo_rest.aclose()
        await real_rest.aclose()
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--until", type=datetime.fromisoformat, required=True)
    parser.add_argument(
        "--horizon-hours",
        type=float,
        default=72,
        help="how long the legs keep working on the production tape after a Demo exit",
    )
    parser.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    args = parser.parse_args()
    if args.since.tzinfo is None or args.until.tzinfo is None:
        parser.error("timestamps must include UTC offsets")
    args.horizon = timedelta(hours=args.horizon_hours)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
