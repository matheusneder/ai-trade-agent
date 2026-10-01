"""Fábricas de clientes ligados à Binance simulada."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx

from tests.support.fake_binance import FakeBinance
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.exchange.signing import HmacSigner

BASE_URL = "https://fake.binance"


@asynccontextmanager
async def fake_api(
    fake: FakeBinance,
    *,
    trading_enabled: bool = True,
    clock: Callable[[], int] | None = None,
) -> AsyncIterator[BinanceSpotApi]:
    """``clock``: relógio local do agente (por padrão, o mesmo da Binance simulada)."""
    async with httpx.AsyncClient(base_url=BASE_URL, transport=fake.transport) as http:
        rest = BinanceRestClient(
            BASE_URL,
            api_key=fake.api_key,
            signer=HmacSigner(fake.secret.decode()),
            trading_enabled=trading_enabled,
            http_client=http,
            clock=clock or fake.clock,
        )
        yield BinanceSpotApi(rest)
