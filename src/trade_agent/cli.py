"""CLI de operação manual (Fase 1): consultas e ordens protegidas no Testnet/Demo.

Exemplos::

    trade-agent info BTCUSDT
    trade-agent account
    trade-agent lists
    trade-agent open BTCUSDT --quote 20 --tp-pct 3 --tp-trailing-bips 100 --stop-pct 4
    trade-agent protect BTCUSDT --qty 0.0003 --tp-pct 3 --tp-trailing-bips 100 --stop-pct 4
    trade-agent close BTCUSDT --qty 0.0003 --list-id ta1-man-0a1b2c3d4e-0-L

Ordens exigem ``TA_TRADING_ENABLED=true``; em produção exigem também ``--confirm-prod``.
"""

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from typing import Any, TextIO

from trade_agent.config.settings import Settings, load_settings
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.environments import BinanceEnvironment
from trade_agent.exchange.errors import BinanceError
from trade_agent.exchange.factory import build_rest_client
from trade_agent.exchange.rules import OrderValidationError
from trade_agent.execution.gateway import ExecutionGateway, OrderOutcomeUnknownError
from trade_agent.execution.ids import new_decision_id, order_ids
from trade_agent.execution.orders import (
    EntryMode,
    EntryOrder,
    ProtectionPolicy,
    StopMode,
    TakeProfitMode,
    build_market_sell,
    build_oco,
    build_opoco,
)
from trade_agent.log import configure_logging

MANUAL_PROFILE = "man"
BIPS = Decimal(10_000)

type ApiFactory = Callable[[Settings], AbstractAsyncContextManager[BinanceSpotApi]]


@asynccontextmanager
async def _default_api(settings: Settings) -> AsyncIterator[BinanceSpotApi]:
    async with build_rest_client(settings) as rest:
        await rest.sync_time()
        yield BinanceSpotApi(rest)


def _add_protection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tp-pct", type=Decimal, required=True, help="alvo/ativação em %%")
    parser.add_argument("--tp-trailing-bips", type=int, help="trailing do take-profit (bips)")
    parser.add_argument("--tp-limit", action="store_true", help="alvo fixo (LIMIT_MAKER)")
    stop = parser.add_mutually_exclusive_group(required=True)
    stop.add_argument("--stop-pct", type=Decimal, help="stop fixo em %% abaixo da referência")
    stop.add_argument("--trailing-stop-bips", type=int, help="stop com trailing (bips)")
    parser.add_argument("--confirm-prod", action="store_true", help="necessário em produção")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trade-agent", description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    commands = parser.add_subparsers(dest="command", required=True)

    info = commands.add_parser("info", help="regras do símbolo e preço atual")
    info.add_argument("symbol")
    commands.add_parser("account", help="saldos não nulos")
    commands.add_parser("lists", help="listas de ordens abertas")

    open_cmd = commands.add_parser("open", help="compra com OPOCO (entrada + OCO nativo)")
    open_cmd.add_argument("symbol")
    open_cmd.add_argument("--quote", type=Decimal, required=True, help="valor na moeda de cotação")
    open_cmd.add_argument("--slippage-bps", type=int, default=20)
    open_cmd.add_argument("--maker-price", type=Decimal, help="entrada LIMIT_MAKER neste preço")
    _add_protection_args(open_cmd)

    protect = commands.add_parser("protect", help="OCO de proteção para saldo existente")
    protect.add_argument("symbol")
    protect.add_argument("--qty", type=Decimal, required=True)
    _add_protection_args(protect)

    close = commands.add_parser("close", help="cancela a proteção e vende a mercado")
    close.add_argument("symbol")
    close.add_argument("--qty", type=Decimal, required=True)
    close.add_argument("--list-id", help="listClientOrderId da proteção ativa")
    close.add_argument("--confirm-prod", action="store_true")
    return parser


def _policy(args: argparse.Namespace) -> ProtectionPolicy:
    return ProtectionPolicy(
        take_profit_mode=TakeProfitMode.LIMIT if args.tp_limit else TakeProfitMode.TRAILING,
        take_profit_pct=args.tp_pct,
        take_profit_trailing_bips=args.tp_trailing_bips,
        stop_mode=StopMode.TRAILING if args.trailing_stop_bips else StopMode.FIXED,
        stop_pct=args.stop_pct,
        stop_trailing_bips=args.trailing_stop_bips,
    )


def _emit(out: TextIO, payload: Any) -> None:
    out.write(json.dumps(payload, indent=2, default=str, ensure_ascii=False) + "\n")


async def run(args: argparse.Namespace, api: BinanceSpotApi, out: TextIO) -> int:
    """Executa o subcomando com uma API já conectada."""
    gateway = ExecutionGateway(api)
    if args.command == "info":
        rules = (await api.exchange_info([args.symbol]))[args.symbol]
        ticker = await api.book_ticker(args.symbol)
        _emit(
            out,
            {"rules": dataclasses.asdict(rules), "bid": ticker.bid_price, "ask": ticker.ask_price},
        )
    elif args.command == "account":
        account = await api.account()
        _emit(out, {b.asset: {"free": b.free, "locked": b.locked} for b in account.balances})
    elif args.command == "lists":
        lists = await api.open_order_lists()
        _emit(out, [ol.model_dump(by_alias=True) for ol in lists])
    elif args.command == "open":
        rules = (await api.exchange_info([args.symbol]))[args.symbol]
        ticker = await api.book_ticker(args.symbol)
        if args.maker_price is not None:
            price, mode = args.maker_price, EntryMode.LIMIT_MAKER_GTC
        else:
            price = ticker.ask_price * (1 + Decimal(args.slippage_bps) / BIPS)
            mode = EntryMode.LIMIT_FOK
        entry = EntryOrder(args.symbol, quantity=args.quote / price, limit_price=price, mode=mode)
        ids = order_ids(MANUAL_PROFILE, new_decision_id())
        params = build_opoco(entry, _policy(args).resolve(price), ids, rules)
        result = await gateway.submit_order_list("opoco", params)
        _emit(out, result.model_dump(by_alias=True))
    elif args.command == "protect":
        rules = (await api.exchange_info([args.symbol]))[args.symbol]
        reference = (await api.book_ticker(args.symbol)).bid_price
        ids = order_ids(MANUAL_PROFILE, new_decision_id())
        params = build_oco(
            args.symbol,
            args.qty,
            _policy(args).resolve(reference),
            ids,
            rules,
            reference_price=reference,
        )
        result = await gateway.submit_order_list("oco", params)
        _emit(out, result.model_dump(by_alias=True))
    else:  # close
        rules = (await api.exchange_info([args.symbol]))[args.symbol]
        reference = (await api.book_ticker(args.symbol)).bid_price
        exit_id = order_ids(MANUAL_PROFILE, new_decision_id()).exit_id
        sell = build_market_sell(args.symbol, args.qty, exit_id, rules, reference_price=reference)
        order = await gateway.close_position(args.symbol, args.list_id, sell)
        _emit(out, order.model_dump(by_alias=True) if order else {"already_closed": True})
    return 0


_ORDER_COMMANDS = frozenset({"open", "protect", "close"})


async def _main_async(
    argv: Sequence[str] | None,
    out: TextIO,
    err: TextIO,
    api_factory: ApiFactory,
) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(env_file=args.env_file)
    configure_logging(settings.log_level, settings.log_format)
    if (
        args.command in _ORDER_COMMANDS
        and settings.binance_env is BinanceEnvironment.PROD
        and not args.confirm_prod
    ):
        err.write("Ambiente de produção: repita o comando com --confirm-prod.\n")
        return 2
    try:
        async with api_factory(settings) as api:
            return await run(args, api, out)
    except OrderValidationError as exc:
        err.write("Ordem inválida:\n" + "\n".join(f"  - {v}" for v in exc.violations) + "\n")
    except OrderOutcomeUnknownError as exc:
        err.write(f"Resultado desconhecido ({exc.client_id}): verifique com `lists`.\n")
    except (BinanceError, ValueError) as exc:
        err.write(f"Erro: {exc}\n")
    return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    api_factory: ApiFactory = _default_api,
) -> int:
    return asyncio.run(_main_async(argv, out, err, api_factory))
