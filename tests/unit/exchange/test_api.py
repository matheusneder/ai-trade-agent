from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import respx

from tests.support.clients import fake_api
from tests.support.fake_binance import FakeBinance, Fault
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceRejectedError, TradingDisabledError
from trade_agent.exchange.models import ListOrderStatus, OrderStatus
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.exchange.signing import HmacSigner

D = Decimal


@pytest.fixture
def fake() -> FakeBinance:
    return FakeBinance(prices={"BTCUSDT": D("63000"), "ETHUSDT": D("2500")})


@pytest.fixture
async def api(fake: FakeBinance) -> AsyncIterator[BinanceSpotApi]:
    async with fake_api(fake) as client:
        yield client


async def test_exchange_info_variants(api: BinanceSpotApi, fake: FakeBinance) -> None:
    single = await api.exchange_info(["BTCUSDT"])
    assert list(single) == ["BTCUSDT"]
    assert single["BTCUSDT"].tick_size == D("0.01")
    several = await api.exchange_info(["BTCUSDT", "ETHUSDT"])
    assert set(several) == {"BTCUSDT", "ETHUSDT"}
    assert fake.calls("GET", "/api/v3/exchangeInfo")[-1].url.query.decode() == (
        "symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D"
    )
    everything = await api.exchange_info()
    assert "SOLUSDT" in everything


async def test_market_data(api: BinanceSpotApi) -> None:
    ticker = await api.book_ticker("BTCUSDT")
    assert ticker.ask_price == D("63000")
    assert await api.avg_price("ETHUSDT") == D("2500")


async def test_account_and_commission(api: BinanceSpotApi) -> None:
    account = await api.account()
    assert account.balance("USDT").free == D("10000")
    rates = await api.commission("BTCUSDT")
    assert rates.taker_rate == D("0.001")


async def test_new_order_get_find_trades(api: BinanceSpotApi) -> None:
    order = await api.new_order(
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": D("0.01"),
            "newClientOrderId": "ta1-mod-abcdef-0-E",
        }
    )
    assert order.status is OrderStatus.FILLED
    fetched = await api.get_order("BTCUSDT", order_id=order.order_id)
    assert fetched.client_order_id == "ta1-mod-abcdef-0-E"
    found = await api.find_order("BTCUSDT", "ta1-mod-abcdef-0-E")
    assert found is not None and found.order_id == order.order_id
    assert await api.find_order("BTCUSDT", "nao-existe") is None
    trades = await api.my_trades("BTCUSDT", order_id=order.order_id)
    assert len(trades) == 1
    assert trades[0].commission_asset == "BTC"


async def test_open_orders_and_cancel(api: BinanceSpotApi) -> None:
    await api.new_order(
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": D("0.01"),
            "price": D("60000"),
            "newClientOrderId": "resting",
        }
    )
    assert [o.client_order_id for o in await api.open_orders("BTCUSDT")] == ["resting"]
    cancelled = await api.cancel_order("BTCUSDT", client_order_id="resting")
    assert cancelled.status is OrderStatus.CANCELED
    assert await api.open_orders() == []


async def test_order_lists_lifecycle(api: BinanceSpotApi) -> None:
    await api.new_order(
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": D("0.01"),
            "newClientOrderId": "buy1",
        }
    )
    placed = await api.place_order_list(
        "oco",
        {
            "symbol": "BTCUSDT",
            "listClientOrderId": "oco1",
            "side": "SELL",
            "quantity": D("0.00999"),
            "aboveType": "LIMIT_MAKER",
            "abovePrice": D("66000"),
            "belowType": "STOP_LOSS",
            "belowStopPrice": D("60000"),
        },
    )
    assert placed.is_active
    assert len(placed.order_reports) == 2
    assert (
        await api.get_order_list(list_client_order_id="oco1")
    ).order_list_id == placed.order_list_id
    assert (await api.get_order_list(order_list_id=placed.order_list_id)).symbol == "BTCUSDT"
    assert [ol.list_client_order_id for ol in await api.open_order_lists()] == ["oco1"]
    found = await api.find_order_list("oco1")
    assert found is not None
    assert await api.find_order_list("nenhuma") is None
    cancelled = await api.cancel_order_list("BTCUSDT", list_client_order_id="oco1")
    assert cancelled.list_order_status is ListOrderStatus.ALL_DONE
    assert await api.open_order_lists() == []


async def test_find_propagates_other_rejections(api: BinanceSpotApi, fake: FakeBinance) -> None:
    fake.inject(Fault("GET", "/api/v3/order", "reject", code=-1100, message="Illegal chars"))
    with pytest.raises(BinanceRejectedError):
        await api.find_order("BTCUSDT", "x")
    fake.inject(Fault("GET", "/api/v3/orderList", "reject", code=-1100, message="Illegal chars"))
    with pytest.raises(BinanceRejectedError):
        await api.find_order_list("x")


async def test_trading_lock_applies_to_order_endpoints(fake: FakeBinance) -> None:
    async with fake_api(fake, trading_enabled=False) as locked:
        with pytest.raises(TradingDisabledError):
            await locked.new_order({"symbol": "BTCUSDT"})
        with pytest.raises(TradingDisabledError):
            await locked.place_order_list("opoco", {"symbol": "BTCUSDT"})
        with pytest.raises(TradingDisabledError):
            await locked.cancel_order_list("BTCUSDT", list_client_order_id="x")
        assert (await locked.account()).can_trade


async def test_wrong_signature_is_rejected_by_fake(fake: FakeBinance) -> None:
    async with BinanceRestClient(
        "https://fake.binance",
        api_key=fake.api_key,
        signer=HmacSigner("outro-segredo"),
        http_client=None,
    ) as rest:
        with respx.mock(base_url="https://fake.binance") as router:
            router.route().mock(side_effect=fake.transport.handle_request)
            with pytest.raises(BinanceRejectedError) as info:
                await BinanceSpotApi(rest).account()
    assert info.value.code == -1022
