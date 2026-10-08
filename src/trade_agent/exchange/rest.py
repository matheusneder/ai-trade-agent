"""Lean asynchronous REST client for Binance Spot.

Responsibilities:

* build and sign requests (percent-encoded payload, identical to what is sent);
* keep the clock offset relative to the server (``timestamp``/``recvWindow``);
* translate error responses into the :mod:`trade_agent.exchange.errors` hierarchy;
* record the limit usage reported by the ``X-MBX-*`` headers;
* block order submission when the ``trading_enabled`` lock is off.

There are no automatic retries in this layer: the retry policy depends on the operation
(query vs order) and belongs to the caller. The exception is ``-1021`` (``timestamp``
outside ``recvWindow``): Binance refuses the request before executing it, so the client
measures the clock again and retries once, orders included.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx
import structlog
from opentelemetry.trace import Span, SpanKind

from trade_agent import tracing
from trade_agent.exchange.errors import (
    INVALID_TIMESTAMP_CODE,
    UNKNOWN_STATUS_CODES,
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
from trade_agent.exchange.serialization import ParamValue, encode_params
from trade_agent.exchange.signing import Signer

log = structlog.get_logger(__name__)

type HttpMethod = Literal["GET", "POST", "PUT", "DELETE"]

API_KEY_HEADER = "X-MBX-APIKEY"
CLOCK_JUMP_WARN_MS = 1000
"""Change of the offset between two measurements that indicates a jump of the local clock."""
CLOCK_SAMPLES = 3
"""Samples per clock measurement; the one with the shortest round trip wins."""

# Failures where the request provably was not sent to the server.
_NOT_SENT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def system_clock_ms() -> int:
    """Local clock in milliseconds since the Unix epoch."""
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class RateLimitUsage:
    """Limit usage reported by Binance in the last response."""

    used_weight_1m: int | None = None
    order_count_10s: int | None = None
    order_count_1d: int | None = None

    def update(self, headers: httpx.Headers) -> None:
        self.used_weight_1m = _int_header(headers, "x-mbx-used-weight-1m", self.used_weight_1m)
        self.order_count_10s = _int_header(headers, "x-mbx-order-count-10s", self.order_count_10s)
        self.order_count_1d = _int_header(headers, "x-mbx-order-count-1d", self.order_count_1d)


class CallHealth:
    """Sliding window of calls for the rate of **infrastructure failures** (no connection,
    unknown outcome, HTTP 5xx, 418 and 429). Business rejections (4xx) do not count as
    failures: they are valid responses from the exchange."""

    def __init__(
        self, *, window_s: float = 300.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._window_s = window_s
        self._clock = clock
        self._calls: list[tuple[float, bool]] = []

    def record(self, ok: bool) -> None:
        now = self._clock()
        self._calls = [c for c in self._calls if now - c[0] <= self._window_s]
        self._calls.append((now, ok))

    def error_rate(self, *, min_calls: int = 5) -> float:
        """Fraction of failures in the window; 0 with fewer than ``min_calls`` calls."""
        now = self._clock()
        recent = [ok for at, ok in self._calls if now - at <= self._window_s]
        if len(recent) < min_calls:
            return 0.0
        return recent.count(False) / len(recent)


def _int_header(headers: httpx.Headers, name: str, default: int | None) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class BinanceRestClient:
    """Binance Spot REST client (asynchronous)."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        signer: Signer | None = None,
        recv_window_ms: int = 5000,
        trading_enabled: bool = False,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = system_clock_ms,
    ) -> None:
        self._http = http_client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._owns_http = http_client is None
        self._api_key = api_key
        self._signer = signer
        self._recv_window_ms = recv_window_ms
        self._trading_enabled = trading_enabled
        self._clock = clock
        self._time_offset_ms = 0
        self._synced = False
        self.usage = RateLimitUsage()
        self.health = CallHealth()

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ properties
    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    @property
    def time_offset_ms(self) -> int:
        """Estimated difference ``server clock - local clock`` in ms."""
        return self._time_offset_ms

    def now_ms(self) -> int:
        """Estimated server time in ms."""
        return self._clock() + self._time_offset_ms

    # ------------------------------------------------------------------ clock
    async def sync_time(self) -> int:
        """Measures the clock offset with ``GET /api/v3/time``; returns the offset.

        The server time is compared with the midpoint of the round trip, and a long round
        trip (opening the connection, with TLS) shifts the estimate by hundreds of ms. That
        is why there are ``CLOCK_SAMPLES`` samples in a row, and the fastest one wins.
        """
        samples: list[tuple[int, int]] = []  # (round trip, offset)
        for _ in range(CLOCK_SAMPLES):
            before = self._clock()
            data = await self.public("GET", "/api/v3/time")
            after = self._clock()
            samples.append((after - before, int(data["serverTime"]) - (before + after) // 2))
        round_trip, offset = min(samples)
        jump = offset - self._time_offset_ms
        if self._synced and abs(jump) > CLOCK_JUMP_WARN_MS:
            # the local clock was adjusted (NTP turned back on, a VM that woke up): the previous
            # offset no longer held, and signed requests would be refused with -1021
            log.warning("rest.clock_jumped", offset_ms=offset, jump_ms=jump)
        self._time_offset_ms, self._synced = offset, True
        log.debug("rest.clock_synced", offset_ms=offset, round_trip_ms=round_trip)
        return offset

    # ------------------------------------------------------------------ requests
    async def public(
        self,
        method: HttpMethod,
        path: str,
        params: Mapping[str, ParamValue] | None = None,
    ) -> Any:
        """Unsigned request (market data, general information)."""
        query = encode_params(params or {})
        url = f"{path}?{query}" if query else path
        return await self._send(method, url, headers={})

    async def signed(
        self,
        method: HttpMethod,
        path: str,
        params: Mapping[str, ParamValue] | None = None,
        *,
        trading: bool = False,
    ) -> Any:
        """Signed request (``TRADE``/``USER_DATA``).

        ``trading=True`` marks operations that create/cancel orders: they are blocked when
        the ``trading_enabled`` lock is off.
        """
        if trading and not self._trading_enabled:
            raise TradingDisabledError(f"envio de ordens desabilitado: {method} {path}")
        if not self._api_key or self._signer is None:
            raise BinanceConfigurationError("requisição assinada exige api_key e signer")
        try:
            return await self._send_signed(method, path, params, self._api_key, self._signer)
        except BinanceTimestampError:
            # refused before executing: measure the clock again and sign once more
            log.warning(
                "rest.timestamp_rejected",
                method=method,
                path=path,
                offset_ms=self._time_offset_ms,
            )
            await self.sync_time()
            return await self._send_signed(method, path, params, self._api_key, self._signer)

    async def _send_signed(
        self,
        method: HttpMethod,
        path: str,
        params: Mapping[str, ParamValue] | None,
        api_key: str,
        signer: Signer,
    ) -> Any:
        full_params: dict[str, ParamValue] = dict(params or {})
        full_params["recvWindow"] = self._recv_window_ms
        full_params["timestamp"] = self.now_ms()
        payload = encode_params(full_params)
        signature = quote(signer.sign(payload), safe="")
        url = f"{path}?{payload}&signature={signature}"
        return await self._send(method, url, headers={API_KEY_HEADER: api_key})

    async def _send(self, method: HttpMethod, url: str, headers: dict[str, str]) -> Any:
        # only the path goes to logs, spans and error messages: the query carries the signature
        path = url.split("?", 1)[0]
        attributes: dict[str, tracing.AttributeValue] = {
            "http.request.method": method,
            "url.path": path,
            "server.address": self._http.base_url.host,
            "peer.service": "binance",
            "trade_agent.signed": API_KEY_HEADER in headers,
        }
        with tracing.span(
            "exchange", f"{method} {path}", kind=SpanKind.CLIENT, attributes=attributes
        ) as span:
            return await self._request(method, url, path, headers, span)

    async def _request(
        self, method: HttpMethod, url: str, path: str, headers: dict[str, str], span: Span
    ) -> Any:
        started = time.monotonic()
        try:
            response = await self._http.request(method, url, headers=headers)
        except _NOT_SENT_ERRORS as exc:
            self.health.record(ok=False)
            span.set_attribute("error.type", type(exc).__name__)
            log.debug("rest.request_failed", method=method, path=path, error=type(exc).__name__)
            raise BinanceConnectionError(f"falha de conexão em {method} {path}: {exc!r}") from exc
        except httpx.TransportError as exc:
            self.health.record(ok=False)
            span.set_attribute("error.type", type(exc).__name__)
            log.debug("rest.request_failed", method=method, path=path, error=type(exc).__name__)
            raise BinanceUnknownStatusError(
                f"resultado desconhecido em {method} {path}: {exc!r}"
            ) from exc

        status = response.status_code
        self.health.record(ok=status < 500 and status not in {418, 429})
        self.usage.update(response.headers)
        span.set_attribute("http.response.status_code", status)
        if self.usage.used_weight_1m is not None:
            span.set_attribute("trade_agent.used_weight_1m", self.usage.used_weight_1m)
        log.debug(
            "rest.request",
            method=method,
            path=path,
            status=status,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            weight_1m=self.usage.used_weight_1m,
            signed=API_KEY_HEADER in headers,
        )
        if response.is_success:
            return response.json()
        raise self._error_from(response)

    @staticmethod
    def _error_from(response: httpx.Response) -> Exception:
        status = response.status_code
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        code: int | None = None
        message = response.reason_phrase
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = raw_code if isinstance(raw_code, int) else None
            message = str(payload.get("msg", message))

        if status == 418:
            return BinanceIPBannedError(
                status, code, message, payload, retry_after=_retry_after(response.headers)
            )
        if status == 429:
            return BinanceRateLimitedError(
                status, code, message, payload, retry_after=_retry_after(response.headers)
            )
        if status >= 500 or code in UNKNOWN_STATUS_CODES:
            return BinanceUnknownStatusError(
                f"HTTP {status} code={code}: {message}", status=status, code=code
            )
        if code == INVALID_TIMESTAMP_CODE:
            return BinanceTimestampError(status, code, message, payload)
        if 400 <= status < 500:
            return BinanceRejectedError(status, code, message, payload)
        return BinanceAPIError(status, code, message, payload)
