"""Fachada tipada dos endpoints da Binance Spot usados pelo agente."""

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal

from trade_agent.exchange.errors import NO_SUCH_ORDER_CODE, BinanceRejectedError
from trade_agent.exchange.models import (
    Account,
    BookTicker,
    CommissionRates,
    Order,
    OrderList,
    Trade,
)
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.exchange.rules import SymbolRules
from trade_agent.exchange.serialization import ParamValue

type OrderListKind = Literal["oco", "oto", "otoco", "opo", "opoco"]


def _is_not_found(exc: BinanceRejectedError) -> bool:
    return exc.code == NO_SUCH_ORDER_CODE


class BinanceSpotApi:
    """Endpoints REST tipados (dados de mercado, conta e ordens)."""

    def __init__(self, rest: BinanceRestClient) -> None:
        self.rest = rest

    # ------------------------------------------------------------------ mercado
    async def exchange_info(self, symbols: Sequence[str] | None = None) -> dict[str, SymbolRules]:
        """Regras dos símbolos informados (ou de todos os símbolos SPOT)."""
        params: dict[str, ParamValue]
        if not symbols:
            params = {"permissions": "SPOT"}
        elif len(symbols) == 1:
            params = {"symbol": symbols[0]}
        else:
            params = {"symbols": json.dumps(list(symbols), separators=(",", ":"))}
        data = await self.rest.public("GET", "/api/v3/exchangeInfo", params)
        return {item["symbol"]: SymbolRules.from_exchange_info(item) for item in data["symbols"]}

    async def book_ticker(self, symbol: str) -> BookTicker:
        data = await self.rest.public("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return BookTicker.model_validate(data)

    async def avg_price(self, symbol: str) -> Decimal:
        """Preço médio ponderado dos últimos minutos (base do ``PERCENT_PRICE_BY_SIDE``)."""
        data = await self.rest.public("GET", "/api/v3/avgPrice", {"symbol": symbol})
        return Decimal(data["price"])

    # ------------------------------------------------------------------ conta
    async def account(self) -> Account:
        data = await self.rest.signed("GET", "/api/v3/account", {"omitZeroBalances": True})
        return Account.model_validate(data)

    async def commission(self, symbol: str) -> CommissionRates:
        data = await self.rest.signed("GET", "/api/v3/account/commission", {"symbol": symbol})
        return CommissionRates.model_validate(data)

    async def my_trades(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        from_id: int | None = None,
        start_time: int | None = None,
        limit: int = 500,
    ) -> list[Trade]:
        data = await self.rest.signed(
            "GET",
            "/api/v3/myTrades",
            {
                "symbol": symbol,
                "orderId": order_id,
                "fromId": from_id,
                "startTime": start_time,
                "limit": limit,
            },
        )
        return [Trade.model_validate(item) for item in data]

    # ------------------------------------------------------------------ ordens simples
    async def new_order(self, params: Mapping[str, ParamValue]) -> Order:
        data = await self.rest.signed("POST", "/api/v3/order", params, trading=True)
        return Order.model_validate(data)

    async def get_order(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> Order:
        data = await self.rest.signed(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
        )
        return Order.model_validate(data)

    async def find_order(self, symbol: str, client_order_id: str) -> Order | None:
        """Consulta por ``clientOrderId``; ``None`` se a ordem não existir."""
        try:
            return await self.get_order(symbol, client_order_id=client_order_id)
        except BinanceRejectedError as exc:
            if _is_not_found(exc):
                return None
            raise

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        data = await self.rest.signed("GET", "/api/v3/openOrders", {"symbol": symbol})
        return [Order.model_validate(item) for item in data]

    async def cancel_order(self, symbol: str, *, client_order_id: str) -> Order:
        data = await self.rest.signed(
            "DELETE",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            trading=True,
        )
        return Order.model_validate(data)

    # ------------------------------------------------------------------ listas de ordens
    async def place_order_list(
        self, kind: OrderListKind, params: Mapping[str, ParamValue]
    ) -> OrderList:
        data = await self.rest.signed("POST", f"/api/v3/orderList/{kind}", params, trading=True)
        return OrderList.model_validate(data)

    async def get_order_list(
        self,
        *,
        list_client_order_id: str | None = None,
        order_list_id: int | None = None,
    ) -> OrderList:
        data = await self.rest.signed(
            "GET",
            "/api/v3/orderList",
            {"orderListId": order_list_id, "origClientOrderId": list_client_order_id},
        )
        return OrderList.model_validate(data)

    async def find_order_list(self, list_client_order_id: str) -> OrderList | None:
        """Consulta por ``listClientOrderId``; ``None`` se a lista não existir."""
        try:
            return await self.get_order_list(list_client_order_id=list_client_order_id)
        except BinanceRejectedError as exc:
            if _is_not_found(exc):
                return None
            raise

    async def open_order_lists(self) -> list[OrderList]:
        data = await self.rest.signed("GET", "/api/v3/openOrderList")
        return [OrderList.model_validate(item) for item in data]

    async def cancel_order_list(
        self,
        symbol: str,
        *,
        list_client_order_id: str | None = None,
        order_list_id: int | None = None,
    ) -> OrderList:
        data = await self.rest.signed(
            "DELETE",
            "/api/v3/orderList",
            {
                "symbol": symbol,
                "orderListId": order_list_id,
                "listClientOrderId": list_client_order_id,
            },
            trading=True,
        )
        return OrderList.model_validate(data)
