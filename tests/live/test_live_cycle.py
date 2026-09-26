"""Ciclo completo contra o Spot Testnet/Demo Mode (critério de saída da Fase 1).

Executar com ``uv run pytest -m live``. Requer chaves no ``.env`` e recusa produção.
Cada execução usa ~15 USDT de saldo fictício e devolve a posição ao final.
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from trade_agent.config.settings import load_settings
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.environments import BinanceEnvironment
from trade_agent.exchange.factory import build_rest_client
from trade_agent.exchange.models import OrderStatus
from trade_agent.execution.gateway import ExecutionGateway
from trade_agent.execution.ids import new_decision_id, order_ids
from trade_agent.execution.orders import (
    EntryOrder,
    FixedStop,
    Protection,
    TrailingTakeProfit,
    build_market_sell,
    build_oco,
    build_opoco,
)

D = Decimal
SYMBOL = "BTCUSDT"
ENV_FILE = Path(".env")


HAS_ENV_FILE = ENV_FILE.exists()


@pytest.fixture
async def api() -> AsyncIterator[BinanceSpotApi]:
    if not HAS_ENV_FILE:
        pytest.skip("sem .env")
    settings = load_settings(env_file=ENV_FILE)
    if not settings.has_credentials:
        pytest.skip("sem credenciais no .env")
    if settings.binance_env is BinanceEnvironment.PROD:
        pytest.skip("testes live nunca rodam em produção")
    settings = settings.model_copy(update={"trading_enabled": True})
    async with build_rest_client(settings) as rest:
        await rest.sync_time()
        yield BinanceSpotApi(rest)


async def test_open_adjust_close_cycle(api: BinanceSpotApi) -> None:
    gateway = ExecutionGateway(api)
    rules = (await api.exchange_info([SYMBOL]))[SYMBOL]
    ask = (await api.book_ticker(SYMBOL)).ask_price
    assert (await api.account()).balance("USDT").free >= 20, "saldo USDT insuficiente"

    decision = new_decision_id()
    ids = order_ids("live", decision)
    entry_price = ask * D("1.003")
    entry = EntryOrder(SYMBOL, quantity=D(15) / entry_price, limit_price=entry_price)
    protection = Protection(
        TrailingTakeProfit(entry_price * D("1.03"), 100), FixedStop(entry_price * D("0.96"))
    )

    # 1) abrir: OPOCO FOK arma o OCO na própria Binance
    opened = await gateway.submit_order_list("opoco", build_opoco(entry, protection, ids, rules))
    assert opened.is_active
    tp = await api.get_order(SYMBOL, client_order_id=ids.take_profit_id)
    sl = await api.get_order(SYMBOL, client_order_id=ids.stop_id)
    assert tp.status is OrderStatus.NEW
    assert sl.status is OrderStatus.NEW
    assert tp.trailing_delta == 100
    qty = tp.orig_qty

    # 2) ajustar: novo OCO com stop mais próximo (cancelar + recriar)
    bid = (await api.book_ticker(SYMBOL)).bid_price
    new_ids = order_ids("live", decision, seq=1)
    tighter = Protection(TrailingTakeProfit(bid * D("1.04"), 150), FixedStop(bid * D("0.97")))
    replaced = await gateway.replace_protection(
        SYMBOL,
        ids.list_id,
        build_oco(SYMBOL, qty, tighter, new_ids, rules, reference_price=bid),
        build_market_sell(SYMBOL, qty, new_ids.exit_id, rules, reference_price=bid),
    )
    assert replaced.protection is not None and replaced.protection.is_active

    # 3) fechar: cancelar a proteção e vender a mercado
    sell = build_market_sell(SYMBOL, qty, new_ids.exit_id, rules, reference_price=bid)
    closed = await gateway.close_position(SYMBOL, new_ids.list_id, sell)
    assert closed is not None and closed.status is OrderStatus.FILLED
    open_ids = {ol.list_client_order_id for ol in await api.open_order_lists()}
    assert ids.list_id not in open_ids and new_ids.list_id not in open_ids
