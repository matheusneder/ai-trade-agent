"""Execution operations with safety semantics.

* **Idempotent sending:** on an unknown outcome (timeout, 5xx), the order is *looked up*
  by its client ID; it is never resent blindly. Binance accepts a repeated
  ``listClientOrderId`` after the previous list has finished, so resending could
  duplicate a position.
* **Protection swap:** Binance does not replace an *order list* atomically. The old OCO
  is canceled and the new one is created right after. If the new one is rejected, the
  position is sold at market (*fail-safe*), so it is never left unprotected.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import structlog

from trade_agent import tracing
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
    """It was not possible to confirm whether the order was accepted; reconciliation decides."""

    def __init__(self, client_id: str) -> None:
        super().__init__(f"resultado desconhecido para {client_id}")
        self.client_id = client_id


@dataclass(frozen=True, slots=True)
class ProtectionReplacement:
    """Outcome of swapping a position's protection."""

    protection: OrderList | None
    """New active OCO; ``None`` when the *fail-safe* sold the position."""

    fallback_exit: Order | None = None
    """Market sell executed by the *fail-safe*, if any."""

    already_closed: bool = False
    """The previous protection had already closed the position; nothing was sent."""


class ExecutionGateway:
    """Order sending with confirmation of unknown outcomes and a *fail-safe*."""

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

    # ------------------------------------------------------------------ idempotent sending
    @tracing.traced("execution", "order_list.submit")
    async def submit_order_list(
        self, kind: OrderListKind, params: Mapping[str, ParamValue]
    ) -> OrderList:
        list_id = str(params["listClientOrderId"])
        log.debug("order_list.submit", kind=kind, list_id=list_id, symbol=params.get("symbol"))
        return await self._submit(
            list_id,
            lambda: self.api.place_order_list(kind, params),
            lambda: self.api.find_order_list(list_id),
        )

    @tracing.traced("execution", "order.submit")
    async def submit_order(self, params: Mapping[str, ParamValue]) -> Order:
        symbol, client_id = str(params["symbol"]), str(params["newClientOrderId"])
        log.debug(
            "order.submit",
            symbol=symbol,
            client_id=client_id,
            side=params.get("side"),
            type=params.get("type"),
            quantity=params.get("quantity"),
        )
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
                result = await send()
            except BinanceConnectionError:
                # The request never left the machine: resending is safe.
                if attempt >= self._connection_retries:
                    raise
                attempt += 1
                log.warning("order.connection_retry", client_id=client_id, attempt=attempt)
                await self._sleep(self._confirm_delay_s)
            except BinanceUnknownStatusError:
                log.warning("order.unknown_status", client_id=client_id)
                return await self._confirm(client_id, lookup)
            else:
                log.debug("order.accepted", client_id=client_id, retries=attempt)
                return result

    async def _confirm[T](self, client_id: str, lookup: Callable[[], Awaitable[T | None]]) -> T:
        for attempt in range(self._confirm_attempts):
            if attempt:
                await self._sleep(self._confirm_delay_s)
            try:
                found = await lookup()
            except (BinanceUnknownStatusError, BinanceConnectionError):
                log.debug("order.confirm_lookup_failed", client_id=client_id, attempt=attempt)
                continue
            log.debug(
                "order.confirm_lookup",
                client_id=client_id,
                attempt=attempt,
                found=found is not None,
            )
            if found is not None:
                log.info("order.confirmed_after_unknown", client_id=client_id)
                return found
        raise OrderOutcomeUnknownError(client_id)

    # ------------------------------------------------------------------ cancellation
    @tracing.traced("execution", "order_list.cancel")
    async def cancel_order_list(self, symbol: str, list_client_order_id: str) -> OrderList | None:
        """Cancels the list; ``None`` if it was no longer active (filled/expired)."""
        log.debug("order_list.cancel", symbol=symbol, list_id=list_client_order_id)
        try:
            return await self.api.cancel_order_list(
                symbol, list_client_order_id=list_client_order_id
            )
        except BinanceRejectedError as exc:
            if exc.code in (UNKNOWN_ORDER_CANCEL_CODE, NO_SUCH_ORDER_CODE):
                log.info("order_list.already_closed", list_id=list_client_order_id)
                return None
            raise

    # ------------------------------------------------------------------ protection and exit
    @tracing.traced("execution", "protection.replace")
    async def replace_protection(
        self,
        symbol: str,
        current_list_id: str,
        new_oco_params: Mapping[str, ParamValue],
        fallback_sell_params: Mapping[str, ParamValue],
    ) -> ProtectionReplacement:
        """Cancels the current OCO and creates the new one; sells at market if it is rejected.

        If the current OCO had already finished (TP/SL filled on Binance), nothing is sent
        and the result reports ``already_closed``.
        """
        log.debug(
            "protection.replace",
            symbol=symbol,
            current_list_id=current_list_id,
            new_list_id=new_oco_params.get("listClientOrderId"),
        )
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

    @tracing.traced("execution", "position.close_submit")
    async def close_position(
        self,
        symbol: str,
        protection_list_id: str | None,
        sell_params: Mapping[str, ParamValue],
    ) -> Order | None:
        """Closes the position: cancels the protection (if any) and sells at market.

        Returns ``None`` if the protection had already closed the position on Binance.
        """
        log.debug("position.close_submit", symbol=symbol, list_id=protection_list_id)
        if protection_list_id is not None:
            cancelled = await self.cancel_order_list(symbol, protection_list_id)
            if cancelled is None:
                return None
        return await self.submit_order(sell_params)
