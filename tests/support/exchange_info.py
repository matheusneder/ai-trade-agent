"""Dados reais de ``exchangeInfo`` (capturados em 25/09/2026) para os testes."""

import json
from functools import cache
from pathlib import Path
from typing import Any

from trade_agent.exchange.rules import SymbolRules

FIXTURE = Path(__file__).parents[1] / "fixtures" / "exchange_info.json"


@cache
def exchange_info() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def symbol_data(symbol: str) -> dict[str, Any]:
    for item in exchange_info()["symbols"]:
        if item["symbol"] == symbol:
            return dict(item)
    raise KeyError(symbol)


def rules_for(symbol: str = "BTCUSDT") -> SymbolRules:
    return SymbolRules.from_exchange_info(symbol_data(symbol))
