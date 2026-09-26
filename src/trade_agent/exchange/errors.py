"""Hierarquia de erros da integração com a Binance.

A distinção mais importante para a execução é entre:

* :class:`BinanceRejectedError` — a requisição foi recusada; **nada foi executado**;
* :class:`BinanceUnknownStatusError` — a requisição pode ter sido executada; é obrigatório
  **consultar** o estado (ex.: pelo ``clientOrderId``) antes de qualquer reenvio;
* :class:`BinanceConnectionError` — a requisição não chegou a ser enviada; reenviar é seguro.
"""

from typing import Any

# Códigos em que a Binance informa explicitamente que o status de execução é desconhecido.
UNKNOWN_STATUS_CODES = frozenset({-1006, -1007})
INVALID_TIMESTAMP_CODE = -1021
NO_SUCH_ORDER_CODE = -2013


class BinanceError(Exception):
    """Erro base da integração com a Binance."""


class BinanceConfigurationError(BinanceError):
    """Configuração insuficiente para a operação (ex.: requisição assinada sem chave)."""


class TradingDisabledError(BinanceError):
    """Tentativa de enviar/cancelar ordens com a trava ``trading_enabled`` desligada."""


class BinanceConnectionError(BinanceError):
    """Falha de conexão antes do envio da requisição; reenviar é seguro."""


class BinanceUnknownStatusError(BinanceError):
    """O resultado da requisição é desconhecido (timeout de leitura, 5xx, -1006, -1007)."""

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
    """Resposta de erro da API com status HTTP e, quando houver, código/mensagem da Binance."""

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
    """Requisição recusada (4xx); nada foi executado."""


class BinanceTimestampError(BinanceRejectedError):
    """``-1021``: timestamp fora do ``recvWindow`` — ressincronizar o relógio."""


class BinanceRateLimitedError(BinanceAPIError):
    """HTTP 429: limite de requisições excedido; aguardar ``retry_after`` segundos."""

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
    """HTTP 418: IP banido temporariamente por excesso de requisições."""
