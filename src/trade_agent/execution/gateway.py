"""Operações de execução com semântica de segurança.

* **Envio idempotente:** diante de resultado desconhecido (timeout, 5xx), a ordem é
  *consultada* pelo ID de cliente; nunca é reenviada às cegas. A Binance aceita repetir um
  ``listClientOrderId`` depois que a lista anterior terminou, então reenviar poderia duplicar
  uma posição.
* **Troca de proteção:** a Binance não substitui uma *order list* de forma atômica. O OCO
  antigo é cancelado e o novo é criado em seguida. Se o novo for rejeitado, a posição é
  vendida a mercado (*fail-safe*), para nunca ficar sem proteção.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import structlog

from trade_agent.exchange.api import BinanceSpotApi, OrderListKind
from trade_agent.exchange.errors import (
    BinanceConnectionError,
    BinanceRejectedError,
    BinanceUnknownStatusError,
)
from trade_agent.exchange.models import Order, OrderList
from trade_agent.exchange.serialization import ParamValue

log = structlog.get_logger(__name__)

UNKNOWN_ORDER_CANCEL_CODE = -2011
NO_SUCH_ORDER_CODE = -2013

type Sleep = Callable[[float], Awaitable[None]]


class OrderOutcomeUnknownError(Exception):
    """Não foi possível confirmar se a ordem foi aceita; a reconciliação deve decidir."""

    def __init__(self, client_id: str) -> None:
        super().__init__(f"resultado desconhecido para {client_id}")
        self.client_id = client_id


@dataclass(frozen=True, slots=True)
class ProtectionReplacement:
    """Resultado da troca de proteção de uma posição."""

    protection: OrderList | None
    """Novo OCO ativo; ``None`` quando o *fail-safe* vendeu a posição."""

    fallback_exit: Order | None = None
    """Venda a mercado executada pelo *fail-safe*, se houve."""

    already_closed: bool = False
    """A proteção anterior já havia encerrado a posição; nada foi enviado."""


class ExecutionGateway:
    """Envio de ordens com confirmação de resultado desconhecido e *fail-safe*."""

    def __init__(
        self,
        api: BinanceSpotApi,
        *,
        confirm_attempts: int = 3,
        confirm_delay_s: float = 1.0,
        connection_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.api = api
        self._confirm_attempts = confirm_attempts
        self._confirm_delay_s = confirm_delay_s
        self._connection_retries = connection_retries
        self._sleep = sleep

    # ------------------------------------------------------------------ envio idempotente
    async def submit_order_list(
        self, kind: OrderListKind, params: Mapping[str, ParamValue]
    ) -> OrderList:
        list_id = str(params["listClientOrderId"])
        return await self._submit(
            list_id,
            lambda: self.api.place_order_list(kind, params),
            lambda: self.api.find_order_list(list_id),
        )

    async def submit_order(self, params: Mapping[str, ParamValue]) -> Order:
        symbol, client_id = str(params["symbol"]), str(params["newClientOrderId"])
        return await self._submit(
            client_id,
            lambda: self.api.new_order(params),
            lambda: self.api.find_order(symbol, client_id),
        )

    async def _submit[T](
        self,
        client_id: str,
        send: Callable[[], Awaitable[T]],
        lookup: Callable[[], Awaitable[T | None]],
    ) -> T:
        attempt = 0
        while True:
            try:
                return await send()
            except BinanceConnectionError:
                # A requisição não saiu da máquina: reenviar é seguro.
                if attempt >= self._connection_retries:
                    raise
                attempt += 1
                log.warning("order.connection_retry", client_id=client_id, attempt=attempt)
                await self._sleep(self._confirm_delay_s)
            except BinanceUnknownStatusError:
                log.warning("order.unknown_status", client_id=client_id)
                return await self._confirm(client_id, lookup)

    async def _confirm[T](self, client_id: str, lookup: Callable[[], Awaitable[T | None]]) -> T:
        for attempt in range(self._confirm_attempts):
            if attempt:
                await self._sleep(self._confirm_delay_s)
            try:
                found = await lookup()
            except (BinanceUnknownStatusError, BinanceConnectionError):
                continue
            if found is not None:
                log.info("order.confirmed_after_unknown", client_id=client_id)
                return found
        raise OrderOutcomeUnknownError(client_id)

    # ------------------------------------------------------------------ cancelamento
    async def cancel_order_list(self, symbol: str, list_client_order_id: str) -> OrderList | None:
        """Cancela a lista; ``None`` se ela já não estava ativa (executada/expirada)."""
        try:
            return await self.api.cancel_order_list(
                symbol, list_client_order_id=list_client_order_id
            )
        except BinanceRejectedError as exc:
            if exc.code in (UNKNOWN_ORDER_CANCEL_CODE, NO_SUCH_ORDER_CODE):
                log.info("order_list.already_closed", list_id=list_client_order_id)
                return None
            raise

    # ------------------------------------------------------------------ proteção e saída
    async def replace_protection(
        self,
        symbol: str,
        current_list_id: str,
        new_oco_params: Mapping[str, ParamValue],
        fallback_sell_params: Mapping[str, ParamValue],
    ) -> ProtectionReplacement:
        """Cancela o OCO atual e cria o novo; vende a mercado se o novo for rejeitado.

        Se o OCO atual já tinha terminado (TP/SL executado na Binance), nada é enviado e o
        resultado indica ``already_closed``.
        """
        cancelled = await self.cancel_order_list(symbol, current_list_id)
        if cancelled is None:
            return ProtectionReplacement(protection=None, already_closed=True)
        try:
            protection = await self.submit_order_list("oco", new_oco_params)
        except BinanceRejectedError as exc:
            log.error(
                "protection.replace_rejected_failsafe_sell",
                symbol=symbol,
                code=exc.code,
                message=exc.message,
            )
            exit_order = await self.submit_order(fallback_sell_params)
            return ProtectionReplacement(protection=None, fallback_exit=exit_order)
        return ProtectionReplacement(protection=protection)

    async def close_position(
        self,
        symbol: str,
        protection_list_id: str | None,
        sell_params: Mapping[str, ParamValue],
    ) -> Order | None:
        """Encerra a posição: cancela a proteção (se houver) e vende a mercado.

        Retorna ``None`` se a proteção já havia encerrado a posição na Binance.
        """
        if protection_list_id is not None:
            cancelled = await self.cancel_order_list(symbol, protection_list_id)
            if cancelled is None:
                return None
        return await self.submit_order(sell_params)
