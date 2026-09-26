"""Binance Spot simulada, em memória, para testes de integração.

Implementa (de forma simplificada, mas com semântica fiel ao que o agente usa):

* endpoints públicos: ``time``, ``exchangeInfo``, ``ticker/bookTicker``, ``avgPrice``;
* conta: ``account``, ``account/commission``, ``myTrades``;
* ordens: ``order`` (MARKET, LIMIT GTC/IOC/FOK, LIMIT_MAKER), ``openOrders``;
* listas: ``orderList/opoco``, ``orderList/oco``, ``orderList`` (GET/DELETE),
  ``openOrderList``;
* verificação de assinatura HMAC, ``timestamp``/``recvWindow`` e chave de API;
* motor de gatilhos: ``set_price`` dispara stops, take-profits (com ativação e
  trailing), LIMIT_MAKER e entradas GTC, cancelando a perna irmã do OCO;
* injeção de falhas (timeout antes/depois de executar, 5xx, rejeição, conexão).

Simplificações: livro sem profundidade (execução no último preço), sem ``PERCENT_PRICE``.
"""

import hashlib
import hmac
import itertools
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote

import httpx

from tests.support.exchange_info import exchange_info

D = Decimal
ZERO = D(0)
BIPS = D(10_000)
DEFAULT_COMMISSION = D("0.001")

type FaultKind = Literal[
    "timeout_after", "timeout_before", "server_error_after", "connect_error", "reject"
]


@dataclass
class Fault:
    method: str
    path: str
    kind: FaultKind
    code: int = -1013
    message: str = "Rejeitado pela falha injetada."
    skip: int = 0
    """Quantas requisições correspondentes deixar passar antes de aplicar a falha."""


@dataclass
class FakeOrder:
    symbol: str
    order_id: int
    client_order_id: str
    side: str
    type: str
    orig_qty: Decimal
    price: Decimal = ZERO
    stop_price: Decimal | None = None
    trailing_delta: int | None = None
    time_in_force: str | None = None
    status: str = "NEW"
    executed_qty: Decimal = ZERO
    cumulative_quote: Decimal = ZERO
    order_list_id: int = -1
    expiry_reason: str | None = None
    pending: bool = False  # ordem pendente de uma lista OTO/OPO ainda não armada
    time: int = 0
    # trailing: ``tracking`` indica que o rastreamento começou; ``extreme`` é o topo
    tracking: bool = False
    extreme: Decimal | None = None

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "symbol": self.symbol,
            "orderId": self.order_id,
            "orderListId": self.order_list_id,
            "clientOrderId": self.client_order_id,
            "price": str(self.price),
            "origQty": str(self.orig_qty),
            "executedQty": str(self.executed_qty),
            "cummulativeQuoteQty": str(self.cumulative_quote),
            "status": "PENDING_NEW" if self.pending else self.status,
            "timeInForce": self.time_in_force or "GTC",
            "type": self.type,
            "side": self.side,
            "time": self.time,
            "updateTime": self.time,
        }
        if self.stop_price is not None:
            data["stopPrice"] = str(self.stop_price)
        if self.trailing_delta is not None:
            data["trailingDelta"] = self.trailing_delta
        if self.expiry_reason is not None:
            data["expiryReason"] = self.expiry_reason
        return data

    @property
    def is_open(self) -> bool:
        return self.status in {"NEW", "PARTIALLY_FILLED"} and not self.pending


@dataclass
class FakeOrderList:
    order_list_id: int
    list_client_order_id: str
    symbol: str
    contingency_type: str
    order_ids: list[int]
    status: str = "EXECUTING"  # EXECUTING | ALL_DONE
    status_type: str = "EXEC_STARTED"
    time: int = 0
    working_id: int | None = None
    pending_ids: list[int] = field(default_factory=list)
    opo: bool = False
    locked_base: Decimal = ZERO


class BinanceApiFault(Exception):
    def __init__(self, status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _reject(message: str, code: int = -2010) -> BinanceApiFault:
    return BinanceApiFault(400, code, message)


class FakeBinance:
    """Exchange simulada. Use :attr:`transport` num ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        api_key: str = "fake-key",
        secret: str = "fake-secret",  # noqa: S107 - segredo fictício do simulador
        prices: Mapping[str, Decimal] | None = None,
        balances: Mapping[str, Decimal] | None = None,
        commission_rate: Decimal = DEFAULT_COMMISSION,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.api_key = api_key
        self.secret = secret.encode()
        self.symbols = {s["symbol"]: s for s in exchange_info()["symbols"]}
        self.prices: dict[str, Decimal] = dict(prices or {"BTCUSDT": D("63000")})
        self.free: dict[str, Decimal] = dict(balances or {"USDT": D("10000")})
        self.locked: dict[str, Decimal] = {}
        self.commission_rate = commission_rate
        self.clock = clock or (lambda: 1_790_000_000_000)
        self.orders: dict[int, FakeOrder] = {}
        self.lists: dict[int, FakeOrderList] = {}
        self.trades: list[dict[str, Any]] = []
        self.faults: deque[Fault] = deque()
        self.requests: list[httpx.Request] = []
        self._ids = itertools.count(1)
        self._list_ids = itertools.count(1)
        self._trade_ids = itertools.count(1)
        self.transport = httpx.MockTransport(self._handle)

    # ================================================================== utilidades de teste
    def inject(self, fault: Fault) -> None:
        self.faults.append(fault)

    def balance(self, asset: str) -> tuple[Decimal, Decimal]:
        return self.free.get(asset, D(0)), self.locked.get(asset, D(0))

    def order_by_client_id(self, client_order_id: str) -> FakeOrder | None:
        matches = [o for o in self.orders.values() if o.client_order_id == client_order_id]
        return matches[-1] if matches else None

    def list_by_client_id(self, list_client_order_id: str) -> FakeOrderList | None:
        matches = [
            ol for ol in self.lists.values() if ol.list_client_order_id == list_client_order_id
        ]
        return matches[-1] if matches else None

    def open_lists(self) -> list[FakeOrderList]:
        return [ol for ol in self.lists.values() if ol.status == "EXECUTING"]

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def set_price(self, symbol: str, price: Decimal) -> None:
        """Novo último preço: processa gatilhos de todas as ordens abertas do símbolo."""
        self.prices[symbol] = price
        for order in list(self.orders.values()):
            if order.symbol == symbol and order.is_open:
                self._evaluate(order, price)

    # ================================================================== HTTP
    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault = self._take_fault(request)
        if fault is not None and fault.kind == "connect_error":
            raise httpx.ConnectError("falha injetada", request=request)
        if fault is not None and fault.kind == "timeout_before":
            raise httpx.ReadTimeout("falha injetada", request=request)
        if fault is not None and fault.kind == "reject":
            return httpx.Response(400, json={"code": fault.code, "msg": fault.message})
        try:
            payload = self._dispatch(request)
        except BinanceApiFault as exc:
            return httpx.Response(exc.status, json={"code": exc.code, "msg": exc.message})
        if fault is not None and fault.kind == "timeout_after":
            raise httpx.ReadTimeout("falha injetada após executar", request=request)
        if fault is not None and fault.kind == "server_error_after":
            return httpx.Response(503, json={"code": -1008, "msg": "Server busy"})
        return httpx.Response(200, json=payload, headers={"X-MBX-USED-WEIGHT-1M": "1"})

    def _take_fault(self, request: httpx.Request) -> Fault | None:
        for fault in list(self.faults):
            if fault.method == request.method and fault.path == request.url.path:
                if fault.skip:
                    fault.skip -= 1
                    return None
                self.faults.remove(fault)
                return fault
        return None

    def _params(self, request: httpx.Request) -> dict[str, str]:
        raw = request.url.query.decode()
        return dict(parse_qsl(raw, keep_blank_values=True))

    def _authenticate(self, request: httpx.Request) -> None:
        if request.headers.get("X-MBX-APIKEY") != self.api_key:
            raise BinanceApiFault(401, -2015, "Invalid API-key, IP, or permissions for action.")
        raw = request.url.query.decode()
        payload, _, signature = raw.rpartition("&signature=")
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, unquote(signature)):
            raise BinanceApiFault(400, -1022, "Signature for this request is not valid.")
        params = self._params(request)
        timestamp, window = int(params["timestamp"]), int(params.get("recvWindow", "5000"))
        now = self.clock()
        if timestamp > now + 1000 or now - timestamp > window:
            raise BinanceApiFault(
                400, -1021, "Timestamp for this request is outside of the recvWindow."
            )

    def _dispatch(self, request: httpx.Request) -> Any:
        path, method = request.url.path, request.method
        params = self._params(request)
        public = {
            ("GET", "/api/v3/time"): self._time,
            ("GET", "/api/v3/exchangeInfo"): self._exchange_info,
            ("GET", "/api/v3/ticker/bookTicker"): self._book_ticker,
            ("GET", "/api/v3/avgPrice"): self._avg_price,
        }
        if (method, path) in public:
            return public[(method, path)](params)
        signed = {
            ("GET", "/api/v3/account"): self._account,
            ("GET", "/api/v3/account/commission"): self._commission,
            ("GET", "/api/v3/myTrades"): self._my_trades,
            ("POST", "/api/v3/order"): self._new_order,
            ("GET", "/api/v3/order"): self._get_order,
            ("DELETE", "/api/v3/order"): self._cancel_order,
            ("GET", "/api/v3/openOrders"): self._open_orders,
            ("POST", "/api/v3/orderList/opoco"): self._new_opoco,
            ("POST", "/api/v3/orderList/oco"): self._new_oco,
            ("GET", "/api/v3/orderList"): self._get_list,
            ("DELETE", "/api/v3/orderList"): self._cancel_list,
            ("GET", "/api/v3/openOrderList"): self._open_lists,
        }
        handler = signed.get((method, path))
        if handler is None:
            raise BinanceApiFault(404, -1000, f"endpoint não simulado: {method} {path}")
        self._authenticate(request)
        return handler(params)

    # ================================================================== públicos
    def _time(self, _: dict[str, str]) -> Any:
        return {"serverTime": self.clock()}

    def _exchange_info(self, params: dict[str, str]) -> Any:
        info = exchange_info()
        if "symbol" in params:
            wanted = [params["symbol"]]
        elif "symbols" in params:
            wanted = [s.strip('"') for s in params["symbols"].strip("[]").split(",")]
        else:
            wanted = list(self.symbols)
        return {**info, "symbols": [self.symbols[s] for s in wanted]}

    def _book_ticker(self, params: dict[str, str]) -> Any:
        price = self._price(params["symbol"])
        return {
            "symbol": params["symbol"],
            "bidPrice": str(price),
            "bidQty": "10",
            "askPrice": str(price),
            "askQty": "10",
        }

    def _avg_price(self, params: dict[str, str]) -> Any:
        return {"mins": 5, "price": str(self._price(params["symbol"])), "closeTime": self.clock()}

    # ================================================================== conta
    def _account(self, _: dict[str, str]) -> Any:
        assets = sorted(set(self.free) | set(self.locked))
        return {
            "canTrade": True,
            "updateTime": self.clock(),
            "balances": [
                {
                    "asset": a,
                    "free": str(self.free.get(a, D(0))),
                    "locked": str(self.locked.get(a, D(0))),
                }
                for a in assets
                if self.free.get(a, D(0)) or self.locked.get(a, D(0))
            ],
        }

    def _commission(self, params: dict[str, str]) -> Any:
        rate = str(self.commission_rate)
        return {
            "symbol": params["symbol"],
            "standardCommission": {"maker": rate, "taker": rate, "buyer": "0", "seller": "0"},
            "taxCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
        }

    def _my_trades(self, params: dict[str, str]) -> Any:
        trades = [t for t in self.trades if t["symbol"] == params["symbol"]]
        if "orderId" in params:
            trades = [t for t in trades if t["orderId"] == int(params["orderId"])]
        if "fromId" in params:
            trades = [t for t in trades if t["id"] >= int(params["fromId"])]
        return trades[: int(params.get("limit", "500"))]

    # ================================================================== helpers de mercado
    def _price(self, symbol: str) -> Decimal:
        if symbol not in self.symbols:
            raise BinanceApiFault(400, -1121, "Invalid symbol.")
        return self.prices[symbol]

    def _assets(self, symbol: str) -> tuple[str, str]:
        data = self.symbols[symbol]
        return data["baseAsset"], data["quoteAsset"]

    def _filters(self, symbol: str) -> dict[str, dict[str, Any]]:
        return {f["filterType"]: f for f in self.symbols[symbol]["filters"]}

    def _check_qty(self, symbol: str, qty: Decimal, price: Decimal) -> None:
        filters = self._filters(symbol)
        lot = filters["LOT_SIZE"]
        step, min_qty = D(lot["stepSize"]), D(lot["minQty"])
        if qty < min_qty or (qty - min_qty) % step != 0:
            raise _reject("Filter failure: LOT_SIZE", -1013)
        if qty * price < D(filters["NOTIONAL"]["minNotional"]):
            raise _reject("Filter failure: NOTIONAL", -1013)

    def _check_price(self, symbol: str, price: Decimal) -> None:
        tick = D(self._filters(symbol)["PRICE_FILTER"]["tickSize"])
        if price <= 0 or price % tick != 0:
            raise _reject("Filter failure: PRICE_FILTER", -1013)

    def _move(self, asset: str, amount: Decimal, *, from_locked: bool = False) -> None:
        book = self.locked if from_locked else self.free
        book[asset] = book.get(asset, D(0)) + amount

    def _lock(self, asset: str, amount: Decimal) -> None:
        if self.free.get(asset, D(0)) < amount:
            raise _reject("Account has insufficient balance for requested action.")
        self._move(asset, -amount)
        self._move(asset, amount, from_locked=True)

    def _unlock(self, asset: str, amount: Decimal) -> None:
        self._move(asset, -amount, from_locked=True)
        self._move(asset, amount)

    def _new_id(self) -> int:
        return next(self._ids)

    def _ensure_unique_client_id(self, client_id: str | None) -> None:
        if client_id and any(
            o.client_order_id == client_id and (o.is_open or o.pending)
            for o in self.orders.values()
        ):
            raise _reject("Duplicate order sent.")

    def _fill(self, order: FakeOrder, price: Decimal, *, maker: bool, from_locked: bool) -> Decimal:
        """Executa a ordem inteira; retorna a quantidade líquida recebida (compras)."""
        base, quote = self._assets(order.symbol)
        qty = order.orig_qty
        quote_amount = qty * price
        commission: Decimal
        if order.side == "BUY":
            spend = quote_amount
            if from_locked:
                reserved = qty * order.price
                self._move(quote, -reserved, from_locked=True)
                self._move(quote, reserved - spend)
            elif self.free.get(quote, D(0)) < spend:
                raise _reject("Account has insufficient balance for requested action.")
            else:
                self._move(quote, -spend)
            commission = qty * self.commission_rate
            received = qty - commission
            self._move(base, received)
            commission_asset = base
        else:
            if from_locked:
                self._move(base, -qty, from_locked=True)
            elif self.free.get(base, D(0)) < qty:
                raise _reject("Account has insufficient balance for requested action.")
            else:
                self._move(base, -qty)
            commission = quote_amount * self.commission_rate
            received = quote_amount - commission
            self._move(quote, received)
            commission_asset = quote
        order.status = "FILLED"
        order.executed_qty = qty
        order.cumulative_quote = quote_amount
        self.trades.append(
            {
                "symbol": order.symbol,
                "id": next(self._trade_ids),
                "orderId": order.order_id,
                "orderListId": order.order_list_id,
                "price": str(price),
                "qty": str(qty),
                "quoteQty": str(quote_amount),
                "commission": str(commission),
                "commissionAsset": commission_asset,
                "time": self.clock(),
                "isBuyer": order.side == "BUY",
                "isMaker": maker,
                "isBestMatch": True,
            }
        )
        return received

    # ================================================================== ordens simples
    def _new_order(self, params: dict[str, str]) -> Any:
        symbol = params["symbol"]
        price_now = self._price(symbol)
        order_type, side = params["type"], params["side"]
        qty = D(params["quantity"])
        self._ensure_unique_client_id(params.get("newClientOrderId"))
        limit = D(params.get("price", "0"))
        self._check_qty(symbol, qty, limit if limit > 0 else price_now)
        order = FakeOrder(
            symbol=symbol,
            order_id=self._new_id(),
            client_order_id=params.get("newClientOrderId") or f"auto{len(self.orders) + 1}",
            side=side,
            type=order_type,
            orig_qty=qty,
            price=limit,
            time_in_force=params.get("timeInForce"),
            time=self.clock(),
        )
        base, quote = self._assets(symbol)
        if order_type == "MARKET":
            self._fill(order, price_now, maker=False, from_locked=False)
        elif order_type in {"LIMIT", "LIMIT_MAKER"}:
            self._check_price(symbol, limit)
            crosses = limit >= price_now if side == "BUY" else limit <= price_now
            if order_type == "LIMIT_MAKER" and crosses:
                raise _reject("Order would immediately match and take.")
            if crosses:
                self._fill(order, price_now, maker=False, from_locked=False)
            elif order.time_in_force in {"FOK", "IOC"}:
                order.status = "EXPIRED"
                order.expiry_reason = (
                    "UNFILLED_FOK_ORDER_EXPIRED"
                    if order.time_in_force == "FOK"
                    else "UNFILLED_IOC_QUANTITY_EXPIRED"
                )
            elif side == "BUY":
                self._lock(quote, qty * limit)
            else:
                self._lock(base, qty)
        else:
            raise _reject("Unsupported order combination", -1014)
        self.orders[order.order_id] = order
        return order.as_json() | {"transactTime": self.clock(), "fills": []}

    def _lookup_order(self, params: dict[str, str]) -> FakeOrder:
        order: FakeOrder | None = None
        if "orderId" in params:
            order = self.orders.get(int(params["orderId"]))
        elif "origClientOrderId" in params:
            order = self.order_by_client_id(params["origClientOrderId"])
        if order is None or order.symbol != params["symbol"]:
            raise _reject("Order does not exist.", -2013)
        return order

    def _get_order(self, params: dict[str, str]) -> Any:
        return self._lookup_order(params).as_json()

    def _cancel_order(self, params: dict[str, str]) -> Any:
        try:
            order = self._lookup_order(params)
        except BinanceApiFault:
            raise _reject("Unknown order sent.", -2011) from None
        if not order.is_open:
            raise _reject("Unknown order sent.", -2011)
        if order.order_list_id != -1:
            raise _reject("Cannot cancel an order of an order list; cancel the list.", -2011)
        self._release(order)
        order.status = "CANCELED"
        return order.as_json()

    def _release(self, order: FakeOrder) -> None:
        base, quote = self._assets(order.symbol)
        if order.type in {"LIMIT", "LIMIT_MAKER"} and order.side == "BUY":
            self._unlock(quote, (order.orig_qty - order.executed_qty) * order.price)
        elif order.side == "SELL" and order.order_list_id == -1:
            self._unlock(base, order.orig_qty - order.executed_qty)

    def _open_orders(self, params: dict[str, str]) -> Any:
        symbol = params.get("symbol")
        return [
            o.as_json()
            for o in self.orders.values()
            if o.is_open and (symbol is None or o.symbol == symbol)
        ]

    # ================================================================== listas
    def _ensure_unique_list_id(self, list_client_id: str) -> None:
        existing = self.list_by_client_id(list_client_id)
        if existing is not None and existing.status == "EXECUTING":
            raise _reject("Duplicate order sent.")

    def _list_json(self, order_list: FakeOrderList, *, reports: bool) -> dict[str, Any]:
        orders = [self.orders[i] for i in order_list.order_ids]
        data: dict[str, Any] = {
            "orderListId": order_list.order_list_id,
            "contingencyType": order_list.contingency_type,
            "listStatusType": order_list.status_type,
            "listOrderStatus": order_list.status,
            "listClientOrderId": order_list.list_client_order_id,
            "transactionTime": order_list.time,
            "symbol": order_list.symbol,
            "orders": [
                {"symbol": o.symbol, "orderId": o.order_id, "clientOrderId": o.client_order_id}
                for o in orders
            ],
        }
        if reports:
            data["orderReports"] = [o.as_json() for o in orders]
        return data

    def _sell_leg(
        self,
        symbol: str,
        prefix: str,
        params: dict[str, str],
        qty: Decimal,
        list_id: int,
        *,
        pending: bool,
    ) -> FakeOrder:
        leg_type = params[f"{prefix}Type"]
        stop = params.get(f"{prefix}StopPrice")
        delta = params.get(f"{prefix}TrailingDelta")
        price = params.get(f"{prefix}Price")
        if leg_type not in {"TAKE_PROFIT", "LIMIT_MAKER", "STOP_LOSS"}:
            raise _reject("Unsupported order combination", -1014)
        if leg_type in {"TAKE_PROFIT", "STOP_LOSS"} and stop is None and delta is None:
            raise _reject(f"Mandatory parameter '{prefix}StopPrice' was not sent", -1102)
        if leg_type == "LIMIT_MAKER" and price is None:
            raise _reject(f"Mandatory parameter '{prefix}Price' was not sent", -1102)
        for value in (stop, price):
            if value is not None:
                self._check_price(symbol, D(value))
        return FakeOrder(
            symbol=symbol,
            order_id=self._new_id(),
            client_order_id=params.get(f"{prefix}ClientOrderId") or f"auto{self._new_id()}",
            side="SELL",
            type=leg_type,
            orig_qty=qty,
            price=D(price) if price else D(0),
            stop_price=D(stop) if stop else None,
            trailing_delta=int(delta) if delta else None,
            order_list_id=list_id,
            pending=pending,
            time=self.clock(),
        )

    def _check_oco_prices(self, above: FakeOrder, below: FakeOrder, last: Decimal) -> None:
        above_ref = above.price if above.type == "LIMIT_MAKER" else above.stop_price
        if above_ref is not None and above_ref <= last:
            raise _reject("The relationship of the prices for the orders is not correct.")
        if below.stop_price is not None and below.stop_price >= last:
            raise _reject("The relationship of the prices for the orders is not correct.")

    def _new_oco(self, params: dict[str, str]) -> Any:
        symbol = params["symbol"]
        last = self._price(symbol)
        base, _ = self._assets(symbol)
        list_client_id = params.get("listClientOrderId") or f"autolist{len(self.lists) + 1}"
        self._ensure_unique_list_id(list_client_id)
        if params["side"] != "SELL":
            raise _reject("somente OCO de venda é simulado", -1014)
        qty = D(params["quantity"])
        self._check_qty(symbol, qty, last)
        list_id = next(self._list_ids)
        above = self._sell_leg(symbol, "above", params, qty, list_id, pending=False)
        below = self._sell_leg(symbol, "below", params, qty, list_id, pending=False)
        self._check_oco_prices(above, below, last)
        self._lock(base, qty)
        for leg in (above, below):
            self._arm(leg, last)
            self.orders[leg.order_id] = leg
        order_list = FakeOrderList(
            order_list_id=list_id,
            list_client_order_id=list_client_id,
            symbol=symbol,
            contingency_type="OCO",
            order_ids=[above.order_id, below.order_id],
            pending_ids=[above.order_id, below.order_id],
            time=self.clock(),
            locked_base=qty,
        )
        self.lists[list_id] = order_list
        return self._list_json(order_list, reports=True)

    def _new_opoco(self, params: dict[str, str]) -> Any:
        symbol = params["symbol"]
        last = self._price(symbol)
        _, quote = self._assets(symbol)
        list_client_id = params.get("listClientOrderId") or f"autolist{len(self.lists) + 1}"
        self._ensure_unique_list_id(list_client_id)
        if params["workingSide"] != "BUY" or params["pendingSide"] != "SELL":
            raise _reject("OPO exige compra na ordem de trabalho e venda nas pendentes", -1014)
        if "pendingQuantity" in params:
            raise _reject("Pending orders should not include the 'pendingQuantity' tag.", -1225)
        qty = D(params["workingQuantity"])
        price = D(params["workingPrice"])
        self._check_price(symbol, price)
        self._check_qty(symbol, qty, price)
        working_type = params["workingType"]
        tif = params.get("workingTimeInForce")
        if working_type == "LIMIT" and tif is None:
            raise _reject("Working order must include the 'workingTimeInForce' tag.", -1224)
        self._ensure_unique_client_id(params.get("workingClientOrderId"))

        list_id = next(self._list_ids)
        working = FakeOrder(
            symbol=symbol,
            order_id=self._new_id(),
            client_order_id=params.get("workingClientOrderId") or f"auto{self._new_id()}",
            side="BUY",
            type=working_type,
            orig_qty=qty,
            price=price,
            time_in_force=tif,
            order_list_id=list_id,
            time=self.clock(),
        )
        above = self._sell_leg(symbol, "pendingAbove", params, qty, list_id, pending=True)
        below = self._sell_leg(symbol, "pendingBelow", params, qty, list_id, pending=True)
        self._check_oco_prices(above, below, price)
        order_list = FakeOrderList(
            order_list_id=list_id,
            list_client_order_id=list_client_id,
            symbol=symbol,
            contingency_type="OTO",
            order_ids=[working.order_id, above.order_id, below.order_id],
            time=self.clock(),
            working_id=working.order_id,
            pending_ids=[above.order_id, below.order_id],
            opo=True,
        )
        crosses = price >= last
        if working_type == "LIMIT_MAKER" and crosses:
            raise _reject("Order would immediately match and take.")
        for order in (working, above, below):
            self.orders[order.order_id] = order
        self.lists[list_id] = order_list
        if crosses:
            received = self._fill(working, last, maker=False, from_locked=False)
            self._activate_pending(order_list, received)
        elif tif in {"FOK", "IOC"}:
            working.status = "EXPIRED"
            working.expiry_reason = (
                "UNFILLED_FOK_ORDER_EXPIRED" if tif == "FOK" else "UNFILLED_IOC_QUANTITY_EXPIRED"
            )
            for pending_id in order_list.pending_ids:
                pending = self.orders[pending_id]
                pending.pending = False
                pending.status = "EXPIRED"
                pending.expiry_reason = "OTO_PHASE_ONE_EXPIRED"
            order_list.status = order_list.status_type = "ALL_DONE"
        else:
            self._lock(quote, qty * price)
        return self._list_json(order_list, reports=True)

    def _activate_pending(self, order_list: FakeOrderList, received: Decimal) -> None:
        """Arma o OCO pendente com a quantidade recebida (semântica OPO)."""
        base, _ = self._assets(order_list.symbol)
        step = D(self._filters(order_list.symbol)["LOT_SIZE"]["stepSize"])
        qty = (received // step) * step
        self._move(base, -qty)
        self._move(base, qty, from_locked=True)
        order_list.locked_base = qty
        last = self.prices[order_list.symbol]
        for pending_id in order_list.pending_ids:
            leg = self.orders[pending_id]
            leg.pending = False
            leg.orig_qty = qty
            self._arm(leg, last)

    def _arm(self, leg: FakeOrder, last: Decimal) -> None:
        if leg.trailing_delta is not None and leg.stop_price is None:
            leg.tracking = True
            leg.extreme = last

    def _get_list(self, params: dict[str, str]) -> Any:
        order_list: FakeOrderList | None = None
        if "orderListId" in params:
            order_list = self.lists.get(int(params["orderListId"]))
        elif "origClientOrderId" in params:
            order_list = self.list_by_client_id(params["origClientOrderId"])
        if order_list is None:
            raise _reject("Order list does not exist.", -2013)
        return self._list_json(order_list, reports=False)

    def _cancel_list(self, params: dict[str, str]) -> Any:
        order_list: FakeOrderList | None = None
        if "orderListId" in params:
            order_list = self.lists.get(int(params["orderListId"]))
        elif "listClientOrderId" in params:
            order_list = self.list_by_client_id(params["listClientOrderId"])
        if (
            order_list is None
            or order_list.status != "EXECUTING"
            or order_list.symbol != params["symbol"]
        ):
            raise _reject("Unknown order list sent.", -2011)
        base, quote = self._assets(order_list.symbol)
        if order_list.working_id is not None:
            working = self.orders[order_list.working_id]
            if working.is_open:
                self._unlock(quote, working.orig_qty * working.price)
                working.status = "CANCELED"
        for pending_id in order_list.pending_ids:
            leg = self.orders[pending_id]
            if leg.is_open or leg.pending:
                leg.status = "CANCELED"
                leg.pending = False
        if order_list.locked_base:
            self._unlock(base, order_list.locked_base)
            order_list.locked_base = D(0)
        order_list.status = order_list.status_type = "ALL_DONE"
        return self._list_json(order_list, reports=True)

    def _open_lists(self, _: dict[str, str]) -> Any:
        return [self._list_json(ol, reports=False) for ol in self.open_lists()]

    # ================================================================== motor de gatilhos
    def _evaluate(self, order: FakeOrder, price: Decimal) -> None:
        if order.order_list_id != -1:
            order_list = self.lists[order.order_list_id]
            if order.order_id == order_list.working_id:
                if price <= order.price:
                    self._unlock_quote_for(order)
                    received = self._fill(order, order.price, maker=True, from_locked=False)
                    self._activate_pending(order_list, received)
                return
            if self._triggered(order, price):
                self._execute_leg(order_list, order, price)
            return
        if order.type in {"LIMIT", "LIMIT_MAKER"}:
            crosses = price <= order.price if order.side == "BUY" else price >= order.price
            if crosses:
                self._fill(order, order.price, maker=True, from_locked=True)

    def _unlock_quote_for(self, order: FakeOrder) -> None:
        _, quote = self._assets(order.symbol)
        self._unlock(quote, order.orig_qty * order.price)

    def _triggered(self, leg: FakeOrder, price: Decimal) -> bool:
        if leg.type == "LIMIT_MAKER":
            return price >= leg.price
        if leg.trailing_delta is None:
            assert leg.stop_price is not None
            return price >= leg.stop_price if leg.type == "TAKE_PROFIT" else price <= leg.stop_price
        if not leg.tracking:
            assert leg.stop_price is not None
            started = (
                price >= leg.stop_price if leg.type == "TAKE_PROFIT" else price <= leg.stop_price
            )
            if started:
                leg.tracking = True
                leg.extreme = price
            return False
        assert leg.extreme is not None
        leg.extreme = max(leg.extreme, price)
        return price <= leg.extreme * (1 - D(leg.trailing_delta) / BIPS)

    def _execute_leg(self, order_list: FakeOrderList, leg: FakeOrder, price: Decimal) -> None:
        fill_price = leg.price if leg.type == "LIMIT_MAKER" else price
        self._fill(leg, fill_price, maker=leg.type == "LIMIT_MAKER", from_locked=True)
        order_list.locked_base = D(0)
        for other_id in order_list.pending_ids:
            other = self.orders[other_id]
            if other is not leg and other.is_open:
                other.status = "EXPIRED"
                other.expiry_reason = "OCO_TRIGGER"
        order_list.status = order_list.status_type = "ALL_DONE"

    # ================================================================== cenários especiais
    def expire_list_legs(self, list_client_order_id: str, reason: str) -> None:
        """Expira as pernas ativas de uma lista sem execução (ex.: *price range rule*)."""
        order_list = self.list_by_client_id(list_client_order_id)
        assert order_list is not None
        base, _ = self._assets(order_list.symbol)
        for pending_id in order_list.pending_ids:
            leg = self.orders[pending_id]
            if leg.is_open:
                leg.status = "EXPIRED"
                leg.expiry_reason = reason
        if order_list.locked_base:
            self._unlock(base, order_list.locked_base)
            order_list.locked_base = D(0)
        order_list.status = order_list.status_type = "ALL_DONE"

    def partially_fill(self, client_order_id: str, qty: Decimal) -> None:
        """Executa parcialmente uma ordem no livro (entrada maker ou perna LIMIT_MAKER)."""
        order = self.order_by_client_id(client_order_id)
        assert order is not None and order.is_open
        base, quote = self._assets(order.symbol)
        price = order.price
        if order.side == "BUY":
            self._move(quote, -qty * price, from_locked=True)
            self._move(base, qty * (1 - self.commission_rate))
            commission, asset = qty * self.commission_rate, base
        else:
            self._move(base, -qty, from_locked=True)
            self._move(quote, qty * price * (1 - self.commission_rate))
            commission, asset = qty * price * self.commission_rate, quote
            order_list = self.lists.get(order.order_list_id)
            if order_list is not None:
                order_list.locked_base -= qty
                for other_id in order_list.pending_ids:
                    other = self.orders[other_id]
                    if other is not order and other.is_open:
                        other.status = "EXPIRED"
                        other.expiry_reason = "OCO_TRIGGER"
        order.status = "PARTIALLY_FILLED"
        order.executed_qty += qty
        order.cumulative_quote += qty * price
        self.trades.append(
            {
                "symbol": order.symbol,
                "id": next(self._trade_ids),
                "orderId": order.order_id,
                "orderListId": order.order_list_id,
                "price": str(price),
                "qty": str(qty),
                "quoteQty": str(qty * price),
                "commission": str(commission),
                "commissionAsset": asset,
                "time": self.clock(),
                "isBuyer": order.side == "BUY",
                "isMaker": True,
                "isBestMatch": True,
            }
        )
