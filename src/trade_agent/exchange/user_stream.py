"""User Data Stream pela WebSocket API (``userDataStream.subscribe.signature``).

* Reconecta automaticamente com *backoff* exponencial; reconecta de imediato ao receber
  ``serverShutdown`` (aviso de desligamento do servidor) ou ``eventStreamTerminated``.
* A cada (re)assinatura emite :class:`StreamConnected`. Eventos podem ter sido perdidos
  enquanto a conexão esteve fora, então o consumidor deve **reconciliar** com a REST API.
* Os *pings* do servidor são respondidos pela biblioteca ``websockets``.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import structlog
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from trade_agent.exchange.models import Balance
from trade_agent.exchange.serialization import ws_signature_payload
from trade_agent.exchange.signing import Signer

log = structlog.get_logger(__name__)

SUBSCRIBE_METHOD = "userDataStream.subscribe.signature"


# ====================================================================== eventos
@dataclass(frozen=True, slots=True)
class StreamConnected:
    subscription_id: int
    reconnected: bool


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    event_time: int
    symbol: str
    client_order_id: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal
    price: Decimal
    stop_price: Decimal
    order_list_id: int
    orig_client_order_id: str
    execution_type: str
    status: str
    reject_reason: str
    order_id: int
    last_filled_qty: Decimal
    cumulative_filled_qty: Decimal
    last_filled_price: Decimal
    commission: Decimal
    commission_asset: str | None
    transaction_time: int
    trade_id: int
    cumulative_quote_qty: Decimal
    trailing_delta: int | None = None
    expiry_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ListStatusEvent:
    event_time: int
    symbol: str
    order_list_id: int
    contingency_type: str
    list_status_type: str
    list_order_status: str
    list_reject_reason: str
    list_client_order_id: str
    transaction_time: int
    orders: tuple[tuple[int, str], ...]
    """Pares ``(orderId, clientOrderId)``."""


@dataclass(frozen=True, slots=True)
class AccountPosition:
    event_time: int
    last_update_time: int
    balances: tuple[Balance, ...]


@dataclass(frozen=True, slots=True)
class BalanceUpdate:
    event_time: int
    asset: str
    delta: Decimal
    clear_time: int


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    event_type: str
    raw: Mapping[str, Any]


type UserEvent = (
    StreamConnected
    | ExecutionReport
    | ListStatusEvent
    | AccountPosition
    | BalanceUpdate
    | UnknownEvent
)

_RECONNECT_NOW = frozenset({"serverShutdown", "eventStreamTerminated"})


def parse_user_event(event: Mapping[str, Any]) -> UserEvent:
    """Converte o objeto ``event`` de uma mensagem do stream no evento tipado."""
    kind = str(event.get("e", ""))
    if kind == "executionReport":
        return ExecutionReport(
            event_time=int(event["E"]),
            symbol=event["s"],
            client_order_id=event["c"],
            side=event["S"],
            order_type=event["o"],
            time_in_force=event["f"],
            quantity=Decimal(event["q"]),
            price=Decimal(event["p"]),
            stop_price=Decimal(event["P"]),
            order_list_id=int(event["g"]),
            orig_client_order_id=event.get("C", ""),
            execution_type=event["x"],
            status=event["X"],
            reject_reason=event["r"],
            order_id=int(event["i"]),
            last_filled_qty=Decimal(event["l"]),
            cumulative_filled_qty=Decimal(event["z"]),
            last_filled_price=Decimal(event["L"]),
            commission=Decimal(event["n"]),
            commission_asset=event.get("N"),
            transaction_time=int(event["T"]),
            trade_id=int(event["t"]),
            cumulative_quote_qty=Decimal(event["Z"]),
            trailing_delta=int(event["d"]) if "d" in event else None,
            expiry_reason=event.get("eR"),
        )
    if kind == "listStatus":
        return ListStatusEvent(
            event_time=int(event["E"]),
            symbol=event["s"],
            order_list_id=int(event["g"]),
            contingency_type=event["c"],
            list_status_type=event["l"],
            list_order_status=event["L"],
            list_reject_reason=event["r"],
            list_client_order_id=event["C"],
            transaction_time=int(event["T"]),
            orders=tuple((int(o["i"]), o["c"]) for o in event.get("O", ())),
        )
    if kind == "outboundAccountPosition":
        return AccountPosition(
            event_time=int(event["E"]),
            last_update_time=int(event["u"]),
            balances=tuple(
                Balance(asset=b["a"], free=Decimal(b["f"]), locked=Decimal(b["l"]))
                for b in event["B"]
            ),
        )
    if kind == "balanceUpdate":
        return BalanceUpdate(
            event_time=int(event["E"]),
            asset=event["a"],
            delta=Decimal(event["d"]),
            clear_time=int(event["T"]),
        )
    return UnknownEvent(event_type=kind, raw=event)


# ====================================================================== conexão
class SubscriptionError(Exception):
    """A Binance recusou a assinatura do stream (ex.: assinatura inválida)."""


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


type Connector = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]


def _default_connector(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
    return ws_connect(url, open_timeout=10, close_timeout=5)


class UserDataStream:
    """Assinatura resiliente do User Data Stream."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        signer: Signer,
        *,
        now_ms: Callable[[], int],
        recv_window_ms: int = 5000,
        connector: Connector = _default_connector,
        response_timeout_s: float = 10.0,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 60.0,
        max_consecutive_failures: int | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._url = ws_url
        self._api_key = api_key
        self._signer = signer
        self._now_ms = now_ms
        self._recv_window_ms = recv_window_ms
        self._connector = connector
        self._response_timeout_s = response_timeout_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._max_failures = max_consecutive_failures
        self._sleep = sleep

    def subscription_request(self, request_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "apiKey": self._api_key,
            "recvWindow": self._recv_window_ms,
            "timestamp": self._now_ms(),
        }
        params["signature"] = self._signer.sign(ws_signature_payload(params))
        return {"id": request_id, "method": SUBSCRIBE_METHOD, "params": params}

    async def _subscribe(self, ws: WebSocketLike) -> int:
        request_id = uuid.uuid4().hex
        await ws.send(json.dumps(self.subscription_request(request_id)))
        async with asyncio.timeout(self._response_timeout_s):
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") != request_id:
                    continue
                if message.get("status") != 200:
                    raise SubscriptionError(str(message.get("error", message)))
                return int(message["result"]["subscriptionId"])

    async def events(self) -> AsyncIterator[UserEvent]:
        """Itera eventos indefinidamente, reconectando quando necessário."""
        failures = 0
        connected_before = False
        while True:
            try:
                async with self._connector(self._url) as ws:
                    subscription_id = await self._subscribe(ws)
                    failures = 0
                    yield StreamConnected(subscription_id, reconnected=connected_before)
                    connected_before = True
                    async for raw in ws:
                        message = json.loads(raw)
                        event = message.get("event")
                        if not isinstance(event, dict):
                            continue  # respostas de requisições
                        if event.get("e") in _RECONNECT_NOW:
                            log.info("user_stream.reconnect_requested", reason=event.get("e"))
                            break
                        yield parse_user_event(event)
                    else:
                        log.warning("user_stream.closed_by_server")
            except (WebSocketException, OSError, TimeoutError, SubscriptionError) as exc:
                failures += 1
                log.warning("user_stream.failure", error=repr(exc), failures=failures)
                if self._max_failures is not None and failures >= self._max_failures:
                    raise
                delay = min(self._backoff_max_s, self._backoff_initial_s * 2 ** (failures - 1))
                await self._sleep(delay)
