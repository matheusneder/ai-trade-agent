import contextlib

import httpx
import pytest

from trade_agent.exchange.errors import BinanceError
from trade_agent.exchange.rest import BinanceRestClient, CallHealth


def test_error_rate_window_and_minimum_calls() -> None:
    now = [0.0]
    health = CallHealth(window_s=60, clock=lambda: now[0])
    for ok in (True, False, True, False):
        health.record(ok=ok)
    assert health.error_rate() == 0.0  # fewer than 5 calls
    health.record(ok=False)
    assert health.error_rate() == pytest.approx(0.6)
    now[0] = 61
    health.record(ok=True)
    assert health.error_rate(min_calls=1) == 0.0  # old ones left the window


@pytest.mark.parametrize(
    ("outcome", "ok"),
    [
        (httpx.Response(200, json={}), True),
        (httpx.Response(400, json={"code": -2010, "msg": "rejeitada"}), True),
        (httpx.Response(429, json={"code": -1003, "msg": "limite"}), False),
        (httpx.Response(503, json={"code": -1008, "msg": "ocupado"}), False),
        (httpx.ConnectError("sem rede"), False),
        (httpx.ReadTimeout("lento"), False),
    ],
)
async def test_rest_client_records_infrastructure_failures(
    outcome: httpx.Response | Exception, ok: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async with httpx.AsyncClient(
        base_url="https://fake", transport=httpx.MockTransport(handler)
    ) as http:
        client = BinanceRestClient("https://fake", http_client=http)
        with contextlib.suppress(BinanceError):
            await client.public("GET", "/api/v3/time")
    assert client.health._calls[0][1] is ok
