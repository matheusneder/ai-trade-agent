from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from trade_agent.exchange.errors import (
    BinanceAPIError,
    BinanceConfigurationError,
    BinanceConnectionError,
    BinanceIPBannedError,
    BinanceRateLimitedError,
    BinanceRejectedError,
    BinanceTimestampError,
    BinanceUnknownStatusError,
    TradingDisabledError,
)
from trade_agent.exchange.rest import (
    CLOCK_SAMPLES,
    BinanceRestClient,
    RateLimitUsage,
    system_clock_ms,
)
from trade_agent.exchange.signing import HmacSigner

BASE = "https://api.test"
SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client() -> AsyncIterator[BinanceRestClient]:
    async with BinanceRestClient(
        BASE,
        api_key="KEY",
        signer=HmacSigner(SECRET),
        recv_window_ms=5000,
        trading_enabled=True,
        clock=lambda: 1499827319559,
    ) as c:
        yield c


async def test_public_request_builds_query_and_tracks_usage(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    route = router.get("/api/v3/depth").respond(
        200,
        json={"lastUpdateId": 1},
        headers={"X-MBX-USED-WEIGHT-1M": "12", "x-mbx-order-count-10s": "3"},
    )
    data = await client.public("GET", "/api/v3/depth", {"symbol": "BTCUSDT", "limit": 5})
    assert data == {"lastUpdateId": 1}
    request = route.calls.last.request
    assert request.url.query == b"symbol=BTCUSDT&limit=5"
    assert "X-MBX-APIKEY" not in request.headers
    assert client.usage.used_weight_1m == 12
    assert client.usage.order_count_10s == 3
    assert client.usage.order_count_1d is None


async def test_public_request_without_params(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    route = router.get("/api/v3/ping").respond(200, json={})
    assert await client.public("GET", "/api/v3/ping") == {}
    assert route.calls.last.request.url.query == b""


async def test_signed_request_matches_binance_doc_signature(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    route = router.post("/api/v3/order").respond(200, json={"orderId": 1})
    await client.signed(
        "POST",
        "/api/v3/order",
        {
            "symbol": "LTCBTC",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": Decimal("1"),
            "price": Decimal("0.1"),
        },
        trading=True,
    )
    request = route.calls.last.request
    assert request.headers["X-MBX-APIKEY"] == "KEY"
    assert request.url.query == (
        b"symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        b"&recvWindow=5000&timestamp=1499827319559"
        b"&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


async def test_signed_request_percent_encodes_signature(router: respx.MockRouter) -> None:
    class PlusSigner:
        def sign(self, payload: str) -> str:
            return "a+b/c="

    route = router.get("/api/v3/account").respond(200, json={})
    async with BinanceRestClient(BASE, api_key="K", signer=PlusSigner(), clock=lambda: 7) as c:
        await c.signed("GET", "/api/v3/account")
    assert route.calls.last.request.url.query.endswith(b"&signature=a%2Bb%2Fc%3D")


async def test_trading_lock_blocks_order_requests(router: respx.MockRouter) -> None:
    async with BinanceRestClient(BASE, api_key="K", signer=HmacSigner("s")) as c:
        assert c.trading_enabled is False
        with pytest.raises(TradingDisabledError):
            await c.signed("POST", "/api/v3/order", {"symbol": "X"}, trading=True)
        router.get("/api/v3/account").respond(200, json={"balances": []})
        assert await c.signed("GET", "/api/v3/account") == {"balances": []}


async def test_signed_request_requires_credentials() -> None:
    async with BinanceRestClient(BASE) as c:
        with pytest.raises(BinanceConfigurationError):
            await c.signed("GET", "/api/v3/account")


async def test_sync_time_uses_the_fastest_of_three_samples(router: respx.MockRouter) -> None:
    """The server time is compared with the midpoint of each round trip. A long round trip
    (the first one, opening the connection) shifts the estimate: the fastest of the three wins."""
    ticks = iter([0, 900, 1_000, 1_100, 2_000, 2_300])  # round trips of 900, 100 and 300 ms
    server = iter([815, 1_100, 2_250])  # offsets from each midpoint: 365, 50 and 100 ms
    route = router.get("/api/v3/time").mock(
        side_effect=lambda _: httpx.Response(200, json={"serverTime": next(server)})
    )
    async with BinanceRestClient(BASE, clock=lambda: next(ticks)) as c:
        with capture_logs() as logs:
            assert await c.sync_time() == 50
        assert c.time_offset_ms == 50
    assert route.call_count == CLOCK_SAMPLES == 3
    synced = next(e for e in logs if e["event"] == "rest.clock_synced")
    assert (synced["offset_ms"], synced["round_trip_ms"]) == (50, 100)


async def test_now_ms_applies_offset(router: respx.MockRouter) -> None:
    router.get("/api/v3/time").respond(200, json={"serverTime": 10_000})
    async with BinanceRestClient(BASE, clock=lambda: 9_000) as c:
        await c.sync_time()
        assert c.now_ms() == 10_000


async def test_sync_time_warns_only_when_the_clock_jumps(router: respx.MockRouter) -> None:
    # +4 s; +0.5 s of drift; +15.5 s (clock corrected): the three samples of each measurement
    server = iter([v for v in (5_000, 5_500, 21_000) for _ in range(CLOCK_SAMPLES)])
    router.get("/api/v3/time").mock(
        side_effect=lambda _: httpx.Response(200, json={"serverTime": next(server)})
    )
    async with BinanceRestClient(BASE, clock=lambda: 1_000) as c:
        with capture_logs() as logs:
            for _ in range(3):
                await c.sync_time()  # the first measurement has nothing to compare with
    jumps = [e for e in logs if e["event"] == "rest.clock_jumped"]
    assert [(e["offset_ms"], e["jump_ms"], e["log_level"]) for e in jumps] == [
        (20_000, 15_500, "warning")
    ]


async def test_timestamp_rejection_resyncs_and_retries_once(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    """``-1021``: Binance refused before executing; retrying is safe, even for an order."""
    router.get("/api/v3/time").respond(200, json={"serverTime": 1499827319559 + 15_000})
    ahead = "Timestamp for this request was 1000ms ahead of the server's time."
    route = router.post("/api/v3/order").mock(
        side_effect=[
            httpx.Response(400, json={"code": -1021, "msg": ahead}),
            httpx.Response(200, json={"orderId": 7}),
        ]
    )
    with capture_logs() as logs:
        params = {"symbol": "BTCUSDT"}
        assert await client.signed("POST", "/api/v3/order", params, trading=True) == {"orderId": 7}
    first, second = (dict(parse_qsl(c.request.url.query.decode())) for c in route.calls)
    assert int(second["timestamp"]) - int(first["timestamp"]) == 15_000  # signed again
    assert client.time_offset_ms == 15_000
    rejected = next(e for e in logs if e["event"] == "rest.timestamp_rejected")
    assert (rejected["path"], rejected["log_level"]) == ("/api/v3/order", "warning")


async def test_timestamp_rejection_after_resync_is_raised(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    clock = router.get("/api/v3/time").respond(200, json={"serverTime": 1499827319559})
    route = router.get("/api/v3/account").mock(
        side_effect=[
            httpx.Response(400, json={"code": -1021, "msg": "outside of the recvWindow"}),
            httpx.Response(400, json={"code": -1021, "msg": "outside of the recvWindow"}),
        ]
    )
    with pytest.raises(BinanceTimestampError):
        await client.signed("GET", "/api/v3/account")
    assert (route.call_count, clock.call_count) == (2, CLOCK_SAMPLES)  # a single retry


def test_system_clock_is_milliseconds() -> None:
    assert 1_700_000_000_000 < system_clock_ms() < 10_000_000_000_000


@pytest.mark.parametrize(
    ("status", "body", "headers", "error", "code"),
    [
        (400, {"code": -2010, "msg": "insufficient balance"}, {}, BinanceRejectedError, -2010),
        (400, {"code": -1021, "msg": "timestamp"}, {}, BinanceTimestampError, -1021),
        (408, {"code": -1007, "msg": "timeout"}, {}, BinanceUnknownStatusError, -1007),
        (400, {"code": -1006, "msg": "unexpected"}, {}, BinanceUnknownStatusError, -1006),
        (503, {"code": -1008, "msg": "busy"}, {}, BinanceUnknownStatusError, -1008),
        (429, {"code": -1003, "msg": "many"}, {"Retry-After": "7"}, BinanceRateLimitedError, -1003),
        (418, {"code": -1003, "msg": "ban"}, {"Retry-After": "120"}, BinanceIPBannedError, -1003),
    ],
)
async def test_error_mapping(
    router: respx.MockRouter,
    client: BinanceRestClient,
    status: int,
    body: dict[str, object],
    headers: dict[str, str],
    error: type[Exception],
    code: int,
) -> None:
    router.get("/api/v3/x").respond(status, json=body, headers=headers)
    with pytest.raises(error) as info:
        await client.public("GET", "/api/v3/x")
    exc = info.value
    assert getattr(exc, "code", None) == code
    if isinstance(exc, BinanceRateLimitedError):
        assert exc.retry_after == float(headers["Retry-After"])


async def test_rejected_error_keeps_payload_and_message(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    router.get("/api/v3/x").respond(400, json={"code": -2013, "msg": "Order does not exist."})
    with pytest.raises(BinanceRejectedError) as info:
        await client.public("GET", "/api/v3/x")
    assert info.value.status == 400
    assert info.value.message == "Order does not exist."
    assert info.value.payload == {"code": -2013, "msg": "Order does not exist."}


async def test_non_json_error_body(router: respx.MockRouter, client: BinanceRestClient) -> None:
    router.get("/api/v3/x").respond(403, text="<html>WAF</html>")
    with pytest.raises(BinanceRejectedError) as info:
        await client.public("GET", "/api/v3/x")
    assert info.value.code is None
    assert info.value.payload == "<html>WAF</html>"
    assert info.value.message == "Forbidden"


async def test_non_integer_code_is_ignored(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    router.get("/api/v3/x").respond(400, json={"code": "weird", "msg": "?"})
    with pytest.raises(BinanceRejectedError) as info:
        await client.public("GET", "/api/v3/x")
    assert info.value.code is None


async def test_rate_limit_without_valid_retry_after(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    router.get("/api/v3/a").respond(429, json={"code": -1003, "msg": "x"})
    router.get("/api/v3/b").respond(
        429, json={"code": -1003, "msg": "x"}, headers={"Retry-After": "soon"}
    )
    for path in ("/api/v3/a", "/api/v3/b"):
        with pytest.raises(BinanceRateLimitedError) as info:
            await client.public("GET", path)
        assert info.value.retry_after is None


async def test_unexpected_status_maps_to_generic_api_error(
    router: respx.MockRouter, client: BinanceRestClient
) -> None:
    router.get("/api/v3/x").respond(302, json={"msg": "moved"})
    with pytest.raises(BinanceAPIError) as info:
        await client.public("GET", "/api/v3/x")
    assert type(info.value) is BinanceAPIError
    assert info.value.message == "moved"


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x")]
)
async def test_not_sent_transport_errors_are_connection_errors(
    router: respx.MockRouter, client: BinanceRestClient, exc: Exception
) -> None:
    router.get("/api/v3/x").mock(side_effect=exc)
    with pytest.raises(BinanceConnectionError):
        await client.public("GET", "/api/v3/x")


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"), httpx.WriteError("x")]
)
async def test_ambiguous_transport_errors_are_unknown_status(
    router: respx.MockRouter, client: BinanceRestClient, exc: Exception
) -> None:
    router.post("/api/v3/order").mock(side_effect=exc)
    with pytest.raises(BinanceUnknownStatusError):
        await client.signed("POST", "/api/v3/order", {"symbol": "X"}, trading=True)


def test_usage_ignores_invalid_header_values() -> None:
    usage = RateLimitUsage(used_weight_1m=5)
    usage.update(httpx.Headers({"x-mbx-used-weight-1m": "abc", "x-mbx-order-count-1d": "9"}))
    assert usage.used_weight_1m == 5
    assert usage.order_count_1d == 9


async def test_injected_http_client_is_not_closed() -> None:
    http = httpx.AsyncClient(base_url=BASE)
    client = BinanceRestClient(BASE, http_client=http)
    await client.aclose()
    assert not http.is_closed
    await http.aclose()


async def test_owned_http_client_is_closed() -> None:
    client = BinanceRestClient(BASE)
    await client.aclose()
    assert client._http.is_closed
