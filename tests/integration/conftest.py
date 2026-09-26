from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest

from tests.support.clients import fake_api
from tests.support.database import fresh_database, postgres_container_url
from tests.support.fake_binance import FakeBinance
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.execution.gateway import ExecutionGateway
from trade_agent.execution.service import PositionService, RulesCache
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Store


async def no_sleep(_: float) -> None:
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
    return ExecutionGateway(api, sleep=no_sleep)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    yield from postgres_container_url()


@pytest.fixture
async def db(postgres_url: str) -> AsyncIterator[Database]:
    async for database in fresh_database(postgres_url):
        yield database


@pytest.fixture
def store(db: Database) -> Store:
    return Store(db)


@pytest.fixture
def service(api: BinanceSpotApi, gateway: ExecutionGateway, store: Store) -> PositionService:
    return PositionService(api, gateway, store, RulesCache(api))
