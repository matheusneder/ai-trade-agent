from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from tests.support.clients import fake_api
from tests.support.fake_binance import FakeBinance
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.execution.gateway import ExecutionGateway


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def fake() -> FakeBinance:
    return FakeBinance(prices={"BTCUSDT": Decimal("63000")}, balances={"USDT": Decimal("10000")})


@pytest.fixture
async def api(fake: FakeBinance) -> AsyncIterator[BinanceSpotApi]:
    async with fake_api(fake) as client:
        yield client


@pytest.fixture
def gateway(api: BinanceSpotApi) -> ExecutionGateway:
    return ExecutionGateway(api, sleep=_no_sleep)
