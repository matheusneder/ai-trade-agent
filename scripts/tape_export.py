"""Read-only export of the recorded A/B tape (D-050) as the JSONL that ``lab.paper`` replays."""

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from trade_agent.config.settings import load_settings
from trade_agent.exchange.serialization import format_decimal
from trade_agent.persistence.db import Database
from trade_agent.persistence.models import TapeDecisionRecord, TapeQuoteRecord
from trade_agent.persistence.store import Store

BATCH = timedelta(days=1)
"""Quotes reach millions of rows: the interval is read and written a day at a time."""


@dataclass
class _Summary:
    decisions: Counter[str] = field(default_factory=Counter)
    quotes: int = 0
    symbols: set[str] = field(default_factory=set)
    dropped_candidates: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None

    def saw(self, at: datetime) -> None:
        self.first_at = self.first_at or at
        self.last_at = at


def _quote_line(record: TapeQuoteRecord) -> dict[str, Any]:
    return {
        "kind": "quote",
        "at": record.at.isoformat(),
        "symbol": record.symbol,
        "bid": format_decimal(record.bid),
        "ask": format_decimal(record.ask),
    }


def _decision_line(record: TapeDecisionRecord, summary: _Summary) -> dict[str, Any]:
    # The planner received them all and the row keeps them, but the replay refuses the whole
    # tape on a candidate with an empty side or a crossed book.
    candidates = [c for c in record.candidates if 0 < Decimal(c["bid"]) <= Decimal(c["ask"])]
    summary.dropped_candidates += len(record.candidates) - len(candidates)
    return {
        "kind": "decision",
        "at": record.at.isoformat(),
        "profile": record.profile,
        "state": record.state,
        "research": record.research,
        "candidates": candidates,
        "exposure_multiplier": format_decimal(record.exposure_multiplier),
    }


async def export(
    db: Database,
    sink: TextIO,
    *,
    since: datetime,
    until: datetime,
    profiles: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Writes ``[since, until)`` to ``sink``: ordered by ``at`` and, within the same instant,
    quotes before decisions. Returns the summary."""
    store = Store(db)
    summary = _Summary()
    start = since
    while start < until:
        end = min(start + BATCH, until)
        events: list[tuple[datetime, int, int, dict[str, Any]]] = []
        for quote in await store.tape_quotes(start, end):
            summary.quotes += 1
            summary.symbols.add(quote.symbol)
            events.append((quote.at, 0, quote.id, _quote_line(quote)))
        for decision in await store.tape_decisions(start, end, profiles=profiles):
            summary.decisions[decision.profile] += 1
            events.append((decision.at, 1, decision.id, _decision_line(decision, summary)))
        events.sort(key=lambda event: event[:3])
        for at, _, _, line in events:
            summary.saw(at)
            # ASCII only: the replay splits the file with str.splitlines(), which also breaks
            # on Unicode separators, and ``research`` carries free text
            sink.write(json.dumps(line, separators=(",", ":")) + "\n")
        start = end
    return {
        "since": since.isoformat(),
        "until_exclusive": until.isoformat(),
        "decisions": dict(sorted(summary.decisions.items())),
        "quotes": summary.quotes,
        "quote_symbols": len(summary.symbols),
        "dropped_candidates": summary.dropped_candidates,
        "first_at": summary.first_at.isoformat() if summary.first_at else None,
        "last_at": summary.last_at.isoformat() if summary.last_at else None,
    }


async def run(args: argparse.Namespace, sink: TextIO) -> dict[str, Any]:
    settings = load_settings(env_file=args.env_file)
    if settings.database_url is None:
        raise ValueError("TA_DATABASE_URL is required")
    db = Database(settings.database_url.get_secret_value())
    try:
        return await export(
            db, sink, since=args.since, until=args.until, profiles=args.profiles or None
        )
    finally:
        await db.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--until", type=datetime.fromisoformat, required=True)
    parser.add_argument(
        "--profiles", nargs="+", help="configuration names; by default, every profile recorded"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.since.tzinfo is None or args.until.tzinfo is None:
        parser.error("timestamps must include UTC offsets")
    if args.since >= args.until:
        parser.error("--since must come before --until")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # A partial file never takes the output's name: the replay would accept a truncated tape.
    partial = args.output.with_name(args.output.name + ".partial")
    with partial.open("w", encoding="utf-8", newline="\n") as sink:
        summary = asyncio.run(run(args, sink))
    partial.replace(args.output)
    print(json.dumps({**summary, "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
