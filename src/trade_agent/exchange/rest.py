"""Cliente REST assíncrono e enxuto para a Binance Spot.

Responsabilidades:

* montar e assinar requisições (payload percent-encoded, idêntico ao que é enviado);
* manter o deslocamento de relógio em relação ao servidor (``timestamp``/``recvWindow``);
* traduzir respostas de erro para a hierarquia de :mod:`trade_agent.exchange.errors`;
* registrar o consumo de limites informado pelos cabeçalhos ``X-MBX-*``;
* impedir envio de ordens quando a trava ``trading_enabled`` estiver desligada.

Não há retentativas automáticas nesta camada: a política de retentativa depende da
operação (consulta x ordem) e fica com quem chama.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx

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

type HttpMethod = Literal["GET", "POST", "PUT", "DELETE"]

API_KEY_HEADER = "X-MBX-APIKEY"

# Falhas em que a requisição comprovadamente não foi enviada ao servidor.
_NOT_SENT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def system_clock_ms() -> int:
    """Relógio local em milissegundos desde a época Unix."""
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class RateLimitUsage:
    """Consumo de limites informado pela Binance na última resposta."""

    used_weight_1m: int | None = None
    order_count_10s: int | None = None
    order_count_1d: int | None = None

    def update(self, headers: httpx.Headers) -> None:
        self.used_weight_1m = _int_header(headers, "x-mbx-used-weight-1m", self.used_weight_1m)
        self.order_count_10s = _int_header(headers, "x-mbx-order-count-10s", self.order_count_10s)
        self.order_count_1d = _int_header(headers, "x-mbx-order-count-1d", self.order_count_1d)


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
    """Cliente REST da Binance Spot (assíncrono)."""

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
        self.usage = RateLimitUsage()

    # ------------------------------------------------------------------ ciclo de vida
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

    # ------------------------------------------------------------------ propriedades
    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    @property
    def time_offset_ms(self) -> int:
        """Diferença estimada ``relógio do servidor - relógio local`` em ms."""
        return self._time_offset_ms

    def now_ms(self) -> int:
        """Horário estimado do servidor em ms."""
        return self._clock() + self._time_offset_ms

    # ------------------------------------------------------------------ relógio
    async def sync_time(self) -> int:
        """Mede o deslocamento de relógio usando ``GET /api/v3/time``; retorna o offset."""
        before = self._clock()
        data = await self.public("GET", "/api/v3/time")
        after = self._clock()
        server_time = int(data["serverTime"])
        self._time_offset_ms = server_time - (before + after) // 2
        return self._time_offset_ms

    # ------------------------------------------------------------------ requisições
    async def public(
        self,
        method: HttpMethod,
        path: str,
        params: Mapping[str, ParamValue] | None = None,
    ) -> Any:
        """Requisição sem assinatura (dados de mercado, informações gerais)."""
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
        """Requisição assinada (``TRADE``/``USER_DATA``).

        ``trading=True`` marca operações que criam/cancelam ordens: são bloqueadas quando a
        trava ``trading_enabled`` está desligada.
        """
        if trading and not self._trading_enabled:
            raise TradingDisabledError(f"envio de ordens desabilitado: {method} {path}")
        if not self._api_key or self._signer is None:
            raise BinanceConfigurationError("requisição assinada exige api_key e signer")

        full_params: dict[str, ParamValue] = dict(params or {})
        full_params["recvWindow"] = self._recv_window_ms
        full_params["timestamp"] = self.now_ms()
        payload = encode_params(full_params)
        signature = quote(self._signer.sign(payload), safe="")
        url = f"{path}?{payload}&signature={signature}"
        return await self._send(method, url, headers={API_KEY_HEADER: self._api_key})

    async def _send(self, method: HttpMethod, url: str, headers: dict[str, str]) -> Any:
        try:
            response = await self._http.request(method, url, headers=headers)
        except _NOT_SENT_ERRORS as exc:
            raise BinanceConnectionError(f"falha de conexão em {method} {url}: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise BinanceUnknownStatusError(
                f"resultado desconhecido em {method} {url}: {exc!r}"
            ) from exc

        self.usage.update(response.headers)
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
