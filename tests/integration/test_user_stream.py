"""User Data Stream against a local WebSocket server that mimics the WebSocket API."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from tests.unit.exchange.test_user_stream_events import EXECUTION_REPORT
from trade_agent.exchange.serialization import ws_signature_payload
from trade_agent.exchange.signing import HmacSigner
from trade_agent.exchange.user_stream import (
    AccountPosition,
    ExecutionReport,
    StreamConnected,
    SubscriptionError,
    UnknownEvent,
    UserDataStream,
    UserEvent,
)

SECRET = "segredo-ws"
SIGNER = HmacSigner(SECRET)

type Script = Callable[[ServerConnection, dict[str, Any], int], Awaitable[None]]


def _verify(request: dict[str, Any]) -> bool:
    params = dict(request["params"])
    signature = params.pop("signature")
    return bool(SIGNER.sign(ws_signature_payload(params)) == signature)


@asynccontextmanager
async def ws_server(script: Script) -> AsyncIterator[str]:
    connections = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        request = json.loads(await ws.recv())
        assert request["method"] == "userDataStream.subscribe.signature"
        assert _verify(request)
        await script(ws, request, connections)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def _ok(ws: ServerConnection, request: dict[str, Any], sub_id: int = 0) -> None:
    await ws.send(json.dumps({"id": "outra", "status": 200, "result": {}}))
    await ws.send(
        json.dumps({"id": request["id"], "status": 200, "result": {"subscriptionId": sub_id}})
    )


def _stream(url: str, sleeps: list[float] | None = None, **kw: Any) -> UserDataStream:
    async def fake_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    return UserDataStream(
        url, "api-key", SIGNER, now_ms=lambda: 1_700_000_000_000, sleep=fake_sleep, **kw
    )


async def _collect(stream: UserDataStream, count: int) -> list[UserEvent]:
    events: list[UserEvent] = []
    async with asyncio.timeout(5):
        async for event in stream.events():
            events.append(event)
            if len(events) == count:
                break
    return events


async def test_subscribes_with_valid_signature_and_parses_events() -> None:
    async def script(ws: ServerConnection, request: dict[str, Any], _: int) -> None:
        assert request["params"]["apiKey"] == "api-key"
        assert request["params"]["recvWindow"] == 5000
        await _ok(ws, request, sub_id=3)
        await ws.send(json.dumps({"subscriptionId": 3, "event": EXECUTION_REPORT}))
        await ws.send(json.dumps({"id": "x", "status": 200, "result": {}}))  # response: ignored
        await ws.send(
            json.dumps(
                {
                    "subscriptionId": 3,
                    "event": {
                        "e": "outboundAccountPosition",
                        "E": 1,
                        "u": 2,
                        "B": [{"a": "BTC", "f": "1", "l": "0"}],
                    },
                }
            )
        )
        await ws.send(
            json.dumps({"subscriptionId": 3, "event": {"e": "externalLockUpdate", "E": 1}})
        )
        await asyncio.sleep(0.3)

    async with ws_server(script) as url:
        events = await _collect(_stream(url), 4)
    assert events[0] == StreamConnected(subscription_id=3, reconnected=False)
    assert isinstance(events[1], ExecutionReport)
    assert isinstance(events[2], AccountPosition)
    assert isinstance(events[3], UnknownEvent)


@pytest.mark.parametrize("reason", ["serverShutdown", "eventStreamTerminated"])
async def test_reconnects_immediately_on_shutdown_notice(reason: str) -> None:
    async def script(ws: ServerConnection, request: dict[str, Any], n: int) -> None:
        await _ok(ws, request, sub_id=n)
        if n == 1:
            await ws.send(json.dumps({"event": {"e": reason, "E": 1}}))
        await asyncio.sleep(0.3)

    sleeps: list[float] = []
    async with ws_server(script) as url:
        events = await _collect(_stream(url, sleeps), 2)
    assert events == [StreamConnected(1, reconnected=False), StreamConnected(2, reconnected=True)]
    assert sleeps == []


async def test_reconnects_after_normal_close_by_server() -> None:
    async def script(ws: ServerConnection, request: dict[str, Any], n: int) -> None:
        await _ok(ws, request, sub_id=n)
        if n > 1:
            await asyncio.sleep(0.3)

    async with ws_server(script) as url:
        events = await _collect(_stream(url), 2)
    assert events[1] == StreamConnected(2, reconnected=True)


async def test_backoff_after_abrupt_failures_then_recovers() -> None:
    async def script(ws: ServerConnection, request: dict[str, Any], n: int) -> None:
        if n <= 2:
            await ws.close(code=1011, reason="erro interno")
            return
        await _ok(ws, request, sub_id=n)
        await asyncio.sleep(0.3)

    sleeps: list[float] = []
    async with ws_server(script) as url:
        events = await _collect(_stream(url, sleeps, backoff_initial_s=0.5, backoff_max_s=0.75), 1)
    assert events == [StreamConnected(3, reconnected=False)]
    assert sleeps == [0.5, 0.75]


async def test_subscription_rejected_gives_up_after_max_failures() -> None:
    async def script(ws: ServerConnection, request: dict[str, Any], _: int) -> None:
        await ws.send(
            json.dumps(
                {
                    "id": request["id"],
                    "status": 400,
                    "error": {"code": -1022, "msg": "Signature invalid"},
                }
            )
        )
        await asyncio.sleep(0.3)

    sleeps: list[float] = []
    async with ws_server(script) as url:
        with pytest.raises(SubscriptionError, match="-1022"):
            await _collect(_stream(url, sleeps, max_consecutive_failures=2), 1)
    assert sleeps == [1.0]


async def test_subscription_response_timeout_counts_as_failure() -> None:
    async def script(ws: ServerConnection, request: dict[str, Any], _: int) -> None:
        await asyncio.sleep(0.5)

    async with ws_server(script) as url:
        with pytest.raises(TimeoutError):
            await _collect(_stream(url, max_consecutive_failures=1, response_timeout_s=0.2), 1)


async def test_connection_refused_is_a_failure() -> None:
    stream = _stream("ws://127.0.0.1:9", max_consecutive_failures=1)
    with pytest.raises(OSError, match=r"\S"):
        await _collect(stream, 1)


def test_subscription_request_is_signed() -> None:
    request = _stream("ws://unused").subscription_request("req-1")
    assert request["id"] == "req-1"
    assert request["method"] == "userDataStream.subscribe.signature"
    assert _verify(request)
