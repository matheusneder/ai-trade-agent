"""Error hierarchy of the Binance integration.

The most important distinction for execution is between:

* :class:`BinanceRejectedError` — the request was refused; **nothing was executed**;
* :class:`BinanceUnknownStatusError` — the request may have been executed; the state must
  be **queried** (e.g. by ``clientOrderId``) before any resend;
* :class:`BinanceConnectionError` — the request was never sent; resending is safe.
"""

from typing import Any

# Codes where Binance explicitly says the execution status is unknown.
UNKNOWN_STATUS_CODES = frozenset({-1006, -1007})
INVALID_TIMESTAMP_CODE = -1021
NO_SUCH_ORDER_CODE = -2013


class BinanceError(Exception):
    """Base error of the Binance integration."""


class BinanceConfigurationError(BinanceError):
    """Insufficient configuration for the operation (e.g. a signed request without a key)."""


class TradingDisabledError(BinanceError):
    """Attempt to send/cancel orders with the ``trading_enabled`` lock off."""


class BinanceConnectionError(BinanceError):
    """Connection failure before the request was sent; resending is safe."""


class BinanceUnknownStatusError(BinanceError):
    """The outcome of the request is unknown (read timeout, 5xx, -1006, -1007)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class BinanceAPIError(BinanceError):
    """API error response with the HTTP status and, when present, Binance's code/message."""

    def __init__(
        self,
        status: int,
        code: int | None,
        message: str,
        payload: Any = None,
    ) -> None:
        super().__init__(f"HTTP {status} code={code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload


class BinanceRejectedError(BinanceAPIError):
    """Request refused (4xx); nothing was executed."""


class BinanceTimestampError(BinanceRejectedError):
    """``-1021``: timestamp outside ``recvWindow`` — resynchronize the clock."""


class BinanceRateLimitedError(BinanceAPIError):
    """HTTP 429: request limit exceeded; wait ``retry_after`` seconds."""

    def __init__(
        self,
        status: int,
        code: int | None,
        message: str,
        payload: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(status, code, message, payload)
        self.retry_after = retry_after


class BinanceIPBannedError(BinanceRateLimitedError):
    """HTTP 418: IP temporarily banned for too many requests."""
