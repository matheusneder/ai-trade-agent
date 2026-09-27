from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx
from structlog.testing import capture_logs

from trade_agent.risk.guard import RiskSnapshot
from trade_agent.telemetry.heartbeat import Heartbeat
from trade_agent.telemetry.recorder import drawdown_pct

URL = "https://hc.example/ping/segredo-123"


async def test_ping_success_and_failures_never_leak_the_url() -> None:
    with respx.mock() as router:
        router.get(URL).mock(
            side_effect=[
                httpx.Response(200),
                httpx.Response(500),
                httpx.ConnectError("sem rede"),
            ]
        )
        async with httpx.AsyncClient() as http:
            heartbeat = Heartbeat(http, URL)
            assert await heartbeat.ping() is True
            with capture_logs() as logs:
                assert await heartbeat.ping() is False
                assert await heartbeat.ping() is False
    assert logs == [
        {"status": 500, "event": "heartbeat.failed", "log_level": "warning"},
        {"error": "ConnectError", "event": "heartbeat.failed", "log_level": "warning"},
    ]
    assert "segredo" not in str(logs)


def test_drawdown_pct() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    snapshot = RiskSnapshot(
        now=now,
        equity=Decimal(900),
        day_start_equity=Decimal(1000),
        peak_equity=Decimal(1200),
        baseline_equity=Decimal(1000),
    )
    assert drawdown_pct(snapshot) == 25.0
    empty = RiskSnapshot(now, Decimal(0), Decimal(0), Decimal(0), Decimal(0))
    assert drawdown_pct(empty) == 0.0
