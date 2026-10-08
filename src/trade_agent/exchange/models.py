"""Typed models of the Binance Spot responses the agent uses.

The models accept extra fields (the API adds fields often) and convert prices and
quantities to :class:`~decimal.Decimal`.
"""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PENDING_NEW = "PENDING_NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    PENDING_CANCEL = "PENDING_CANCEL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"

    @property
    def is_final(self) -> bool:
        """Terminal state: the order no longer changes."""
        return self in _FINAL_ORDER_STATUSES


_FINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.EXPIRED_IN_MATCH,
    }
)


class ListStatusType(StrEnum):
    RESPONSE = "RESPONSE"
    EXEC_STARTED = "EXEC_STARTED"
    UPDATED = "UPDATED"
    ALL_DONE = "ALL_DONE"


class ListOrderStatus(StrEnum):
    EXECUTING = "EXECUTING"
    ALL_DONE = "ALL_DONE"
    REJECT = "REJECT"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class Order(_Model):
    """Order (``GET /api/v3/order`` and the ``orderReports`` of send responses)."""

    symbol: str
    order_id: int = Field(alias="orderId")
    order_list_id: int = Field(default=-1, alias="orderListId")
    client_order_id: str = Field(alias="clientOrderId")
    price: Decimal = Decimal(0)
    orig_qty: Decimal = Field(default=Decimal(0), alias="origQty")
    """Missing from the pending legs of OPO/OPOCO in the send response: the quantity is only
    set when the entry fills (seen on the Spot Testnet on 2026-09-28)."""
    executed_qty: Decimal = Field(default=Decimal(0), alias="executedQty")
    cummulative_quote_qty: Decimal = Field(default=Decimal(0), alias="cummulativeQuoteQty")
    status: OrderStatus
    time_in_force: TimeInForce | None = Field(default=None, alias="timeInForce")
    type: OrderType
    side: OrderSide
    stop_price: Decimal | None = Field(default=None, alias="stopPrice")
    trailing_delta: int | None = Field(default=None, alias="trailingDelta")
    expiry_reason: str | None = Field(default=None, alias="expiryReason")
    update_time: int | None = Field(default=None, alias="updateTime")


class OrderRef(_Model):
    symbol: str
    order_id: int = Field(alias="orderId")
    client_order_id: str = Field(alias="clientOrderId")


class OrderList(_Model):
    """Order list (OCO, OTO, OTOCO, OPO, OPOCO)."""

    order_list_id: int = Field(alias="orderListId")
    contingency_type: str = Field(alias="contingencyType")
    list_status_type: ListStatusType = Field(alias="listStatusType")
    list_order_status: ListOrderStatus = Field(alias="listOrderStatus")
    list_client_order_id: str = Field(alias="listClientOrderId")
    transaction_time: int = Field(alias="transactionTime")
    symbol: str
    orders: tuple[OrderRef, ...] = ()
    order_reports: tuple[Order, ...] = Field(default=(), alias="orderReports")

    @property
    def is_active(self) -> bool:
        return self.list_order_status is ListOrderStatus.EXECUTING


class Balance(_Model):
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class Account(_Model):
    can_trade: bool = Field(default=True, alias="canTrade")
    balances: tuple[Balance, ...] = ()
    update_time: int | None = Field(default=None, alias="updateTime")

    def balance(self, asset: str) -> Balance:
        """Balance of the asset (zero if missing)."""
        for item in self.balances:
            if item.asset == asset:
                return item
        return Balance(asset=asset, free=Decimal(0), locked=Decimal(0))


class BookTicker(_Model):
    symbol: str
    bid_price: Decimal = Field(alias="bidPrice")
    bid_qty: Decimal = Field(alias="bidQty")
    ask_price: Decimal = Field(alias="askPrice")
    ask_qty: Decimal = Field(alias="askQty")


class Ticker24h(_Model):
    """24 h statistics (``GET /api/v3/ticker/24hr``)."""

    symbol: str
    last_price: Decimal = Field(alias="lastPrice")
    price_change_percent: Decimal = Field(alias="priceChangePercent")
    volume: Decimal
    quote_volume: Decimal = Field(alias="quoteVolume")
    count: int = 0


class Trade(_Model):
    """Account trade (``GET /api/v3/myTrades``)."""

    symbol: str
    id: int
    order_id: int = Field(alias="orderId")
    order_list_id: int = Field(default=-1, alias="orderListId")
    price: Decimal
    qty: Decimal
    quote_qty: Decimal = Field(alias="quoteQty")
    commission: Decimal
    commission_asset: str = Field(alias="commissionAsset")
    time: int
    is_buyer: bool = Field(alias="isBuyer")
    is_maker: bool = Field(alias="isMaker")


class CommissionComponent(_Model):
    maker: Decimal = Decimal(0)
    taker: Decimal = Decimal(0)


class CommissionRates(_Model):
    """Account fees for a symbol (``GET /api/v3/account/commission``)."""

    symbol: str
    standard: CommissionComponent = Field(alias="standardCommission")
    special: CommissionComponent | None = Field(default=None, alias="specialCommission")
    tax: CommissionComponent | None = Field(default=None, alias="taxCommission")

    def _total(self, side: str) -> Decimal:
        parts = (self.standard, self.special, self.tax)
        return sum((getattr(part, side) for part in parts if part is not None), Decimal(0))

    @property
    def maker_rate(self) -> Decimal:
        """Total fee of *maker* orders (standard + special + tax), without the BNB discount."""
        return self._total("maker")

    @property
    def taker_rate(self) -> Decimal:
        """Total fee of *taker* orders (standard + special + tax), without the BNB discount."""
        return self._total("taker")
