"""Manual control of the ephemeral SigNoz of the smoke tests (the tests do all this alone).

    uv run python tests/signoz-dashboard-smoke-tests/smoke.py up    # stack, login, dashboards, seed
    uv run python tests/signoz-dashboard-smoke-tests/smoke.py seed --seed 42 --hours 48
    uv run python tests/signoz-dashboard-smoke-tests/smoke.py generate --seed 42 --out summary.json
    uv run python tests/signoz-dashboard-smoke-tests/smoke.py down

``up`` prints the UI address and a throwaway login. ``seed`` sends more data to a running
stack; ``generate`` only builds the data and prints its :class:`Summary` (no Docker).
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # run as a script: the repository root

from tests.support.signoz_smoke import environment, otlp, signoz  # noqa: E402
from tests.support.signoz_smoke.seed import generate  # noqa: E402


def _seed(args: argparse.Namespace, endpoints: environment.Endpoints | None) -> None:
    end = datetime.fromisoformat(args.end) if args.end else None
    data = generate(args.seed, args.hours, end)
    if args.out:
        Path(args.out).write_text(data.summary.to_json(), encoding="utf-8")
    if endpoints is None:
        print(data.summary.to_json())
        return
    requests = otlp.send(data, endpoints.otlp)
    print(
        f"sent {len(data.spans)} spans, {len(data.logs)} logs and "
        f"{data.summary.metric_points} metric points in {requests} OTLP requests"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "seed", "generate"):
        sub = commands.add_parser(name)
        sub.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
        sub.add_argument("--hours", type=float, default=24, help="window length (default 24)")
        sub.add_argument("--end", help="window end, ISO 8601 (default: the current minute)")
        sub.add_argument("--out", help="also write the summary (JSON) to this file")
    commands.add_parser("down")
    args = parser.parse_args(argv)
    stack = environment.Stack()
    if args.command == "down":
        stack.down()
    elif args.command == "generate":
        _seed(args, None)
    elif args.command == "seed":
        _seed(args, stack.endpoints())
    else:
        endpoints = stack.up()
        credentials = signoz.bootstrap(endpoints.ui)
        environment.wait_otlp(endpoints)
        with signoz.api(endpoints.ui, credentials) as http:
            ids = signoz.provision(http)
            _seed(args, endpoints)
        print(f"SigNoz: {endpoints.ui}  (OTLP: {endpoints.otlp})")
        print(f"login: {credentials.email} / {credentials.password}")
        for name, board_id in sorted(ids.items()):
            print(f"  {name}: {endpoints.ui}/dashboard/{board_id}?relativeTime=1d")
        print("stop with: uv run python tests/signoz-dashboard-smoke-tests/smoke.py down")


if __name__ == "__main__":
    main()
