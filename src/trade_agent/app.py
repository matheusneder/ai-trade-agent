"""Raiz de composição: monta os componentes do agente a partir das configurações."""

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from trade_agent.config.settings import Settings
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.environments import endpoints_for
from trade_agent.exchange.errors import BinanceConfigurationError
from trade_agent.exchange.factory import build_rest_client, build_signer
from trade_agent.exchange.user_stream import UserDataStream
from trade_agent.execution.gateway import ExecutionGateway
from trade_agent.execution.service import PositionService, RulesCache
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Store
from trade_agent.reconcile.reconciler import Reconciler
from trade_agent.runtime import AgentRuntime


@asynccontextmanager
async def build_runtime(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    user_stream: bool = True,
) -> AsyncIterator[AgentRuntime]:
    """Cria o runtime completo; libera conexões (HTTP e banco) ao sair."""
    if settings.database_url is None:
        raise BinanceConfigurationError("defina TA_DATABASE_URL para executar o agente")
    signer = build_signer(settings)
    if signer is None or settings.binance_api_key is None:
        raise BinanceConfigurationError("o agente exige credenciais da Binance no .env")
    db = Database(settings.database_url.get_secret_value())
    try:
        async with build_rest_client(settings, http_client=http_client) as rest:
            api = BinanceSpotApi(rest)
            store = Store(db)
            service = PositionService(api, ExecutionGateway(api), store, RulesCache(api))
            stream = UserDataStream(
                endpoints_for(settings.binance_env).ws_api,
                settings.binance_api_key.get_secret_value(),
                signer,
                now_ms=rest.now_ms,
                recv_window_ms=settings.binance_recv_window_ms,
            )
            yield AgentRuntime(
                db=db,
                api=api,
                store=store,
                reconciler=Reconciler(api, service, store),
                events=stream.events if user_stream else None,
            )
    finally:
        await db.dispose()


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT encerram o agente de forma ordenada (onde o SO permite)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


async def run_agent(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    http_client: httpx.AsyncClient | None = None,
    user_stream: bool = True,
) -> None:
    stop = stop or asyncio.Event()
    install_signal_handlers(stop)
    async with build_runtime(settings, http_client=http_client, user_stream=user_stream) as runtime:
        await runtime.run(stop)
