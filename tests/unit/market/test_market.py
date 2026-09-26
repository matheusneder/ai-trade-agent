from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from tests.support.candles import raw_klines, uptrend_with_pullback
from tests.support.clients import fake_api
from tests.support.exchange_info import rules_for, symbol_data
from tests.support.fake_binance import FakeBinance, Fault
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.models import BookTicker, Ticker24h
from trade_agent.exchange.rules import SymbolRules
from trade_agent.market.candles import closed_only, fetch_candles, to_frame
from trade_agent.market.universe import (
    Tier,
    UniverseConfig,
    build_universe,
    prefilter,
    select_universe,
)

D = Decimal


# ============================================================================ candles
def test_to_frame_and_closed_only() -> None:
    frame = uptrend_with_pullback(250)
    rebuilt = to_frame(raw_klines(frame))
    assert list(rebuilt.columns) == list(frame.columns)
    assert rebuilt["close"].iloc[-1] == pytest.approx(frame["close"].iloc[-1])
    assert str(rebuilt.index[0].tz) == "UTC"
    cut = int(frame["close_time"].iloc[-1])
    assert len(closed_only(rebuilt, cut)) == len(frame) - 1


async def test_fetch_candles_drops_open_candle() -> None:
    fake = FakeBinance()
    frame = uptrend_with_pullback(50)
    fake.candles[("BTCUSDT", "1h")] = raw_klines(frame)
    async with fake_api(fake) as api:
        candles = await fetch_candles(
            api, "BTCUSDT", "1h", limit=10, now_ms=int(frame["close_time"].iloc[-1])
        )
        assert len(candles) == 10
        assert candles.index[-1] == frame.index[-2]
        with pytest.raises(ValueError, match="intervalo"):
            await fetch_candles(api, "BTCUSDT", "2h", limit=10, now_ms=0)


# ============================================================================ universo
def _ticker(symbol: str, volume: str) -> Ticker24h:
    return Ticker24h.model_validate(
        {
            "symbol": symbol,
            "lastPrice": "1",
            "priceChangePercent": "0",
            "volume": "1",
            "quoteVolume": volume,
        }
    )


def _book(symbol: str, bid: str, ask: str) -> BookTicker:
    return BookTicker.model_validate(
        {"symbol": symbol, "bidPrice": bid, "bidQty": "1", "askPrice": ask, "askQty": "1"}
    )


def _rules(symbol: str, base: str, **overrides: object) -> SymbolRules:
    data = symbol_data("SOLUSDT")
    data.update(symbol=symbol, baseAsset=base, **overrides)
    return SymbolRules.from_exchange_info(data)


def _market() -> tuple[dict[str, SymbolRules], dict[str, Ticker24h], dict[str, BookTicker]]:
    rules = {
        "BTCUSDT": rules_for("BTCUSDT"),
        "ETHUSDT": rules_for("ETHUSDT"),
        "SOLUSDT": rules_for("SOLUSDT"),
        "AAAUSDT": _rules("AAAUSDT", "AAA"),
        "BBBUSDT": _rules("BBBUSDT", "BBB"),
        "CCCUSDT": _rules("CCCUSDT", "CCC"),
        "FDUSDUSDT": _rules("FDUSDUSDT", "FDUSD"),
        "BTCUPUSDT": _rules("BTCUPUSDT", "BTCUP"),
        "HALTUSDT": _rules("HALTUSDT", "HALT", status="BREAK"),
        "NOOCOUSDT": _rules("NOOCOUSDT", "NOOCO", ocoAllowed=False),
        "NOTRAILUSDT": _rules("NOTRAILUSDT", "NOTRAIL", allowTrailingStop=False),
        "DELUSDT": _rules("DELUSDT", "DEL"),
        "THINUSDT": _rules("THINUSDT", "THIN"),
        "WIDEUSDT": _rules("WIDEUSDT", "WIDE"),
        "NOBOOKUSDT": _rules("NOBOOKUSDT", "NOBOOK"),
        "NEWUSDT": _rules("NEWUSDT", "NEW"),
        "BTCBRL": rules_for("BTCBRL"),
    }
    volumes = {
        "BTCUSDT": "900000000", "ETHUSDT": "500000000", "SOLUSDT": "200000000",
        "AAAUSDT": "90000000", "BBBUSDT": "60000000", "CCCUSDT": "30000000",
        "FDUSDUSDT": "800000000", "BTCUPUSDT": "50000000", "HALTUSDT": "50000000",
        "NOOCOUSDT": "50000000", "NOTRAILUSDT": "50000000", "DELUSDT": "50000000",
        "THINUSDT": "1000", "WIDEUSDT": "50000000", "NOBOOKUSDT": "50000000",
        "NEWUSDT": "70000000", "BTCBRL": "900000000",
    }  # fmt: skip
    tickers = {s: _ticker(s, v) for s, v in volumes.items()}
    books = {s: _book(s, "99.99", "100.01") for s in rules if s != "NOBOOKUSDT"}
    books["WIDEUSDT"] = _book("WIDEUSDT", "99", "101")
    books["NOBOOKUSDT"] = _book("NOBOOKUSDT", "0", "0")
    return rules, tickers, books


CONFIG = UniverseConfig(large_rank=2, mid_rank=3, min_history_days=30)


def test_prefilter_reasons_and_ranking() -> None:
    rules, tickers, books = _market()
    ranked, excluded = prefilter(rules, tickers, books, {"DELUSDT"}, CONFIG)
    assert ranked == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AAAUSDT", "NEWUSDT", "BBBUSDT", "CCCUSDT"]
    assert excluded == {
        "FDUSDUSDT": "stablecoin/fiat",
        "BTCUPUSDT": "token alavancado",
        "HALTUSDT": "status BREAK",
        "NOOCOUSDT": "sem OCO/OTO/OPO",
        "NOTRAILUSDT": "sem trailing stop",
        "DELUSDT": "delistagem agendada",
        "THINUSDT": "volume insuficiente",
        "WIDEUSDT": "spread alto",
        "NOBOOKUSDT": "spread alto",
    }
    assert "BTCBRL" not in excluded  # outra moeda de cotação: fora do escopo


def test_select_universe_assigns_tiers_and_history() -> None:
    rules, tickers, books = _market()
    history = dict.fromkeys(rules, 365) | {"NEWUSDT": 5}
    universe = select_universe(
        rules, tickers, books, delisted=set(), history_days=history, config=CONFIG
    )
    tiers = {m.symbol: m.tier for m in universe.members}
    assert tiers == {
        "BTCUSDT": Tier.CORE, "ETHUSDT": Tier.CORE, "SOLUSDT": Tier.LARGE,
        "AAAUSDT": Tier.LARGE, "BBBUSDT": Tier.MID, "DELUSDT": Tier.SMALL,
        "CCCUSDT": Tier.SMALL,
    }  # fmt: skip
    assert universe.excluded["NEWUSDT"] == "histórico curto"
    assert universe.symbols(frozenset({Tier.CORE})) == ["BTCUSDT", "ETHUSDT"]
    assert universe.by_symbol()["SOLUSDT"].rank == 1
    assert universe.by_symbol()["SOLUSDT"].spread_bps == D(2)


@pytest.fixture
async def market_api() -> AsyncIterator[tuple[FakeBinance, BinanceSpotApi]]:
    fake = FakeBinance(
        prices={
            "BTCUSDT": D("63000"),
            "ETHUSDT": D("2500"),
            "SOLUSDT": D("150"),
            "BTCBRL": D("330000"),
        }
    )
    fake.volumes = {
        "BTCUSDT": D("9e8"),
        "ETHUSDT": D("5e8"),
        "SOLUSDT": D("2e8"),
        "BTCBRL": D("1e8"),
    }
    daily = raw_klines(uptrend_with_pullback(60))
    for symbol in fake.prices:
        fake.candles[(symbol, "1d")] = daily
    fake.candles[("SOLUSDT", "1d")] = daily[:10]
    async with fake_api(fake) as api:
        yield fake, api


async def test_build_universe_from_exchange(market_api: tuple[FakeBinance, BinanceSpotApi]) -> None:
    fake, api = market_api
    fake.delisted = {"ETHUSDT"}
    universe = await build_universe(api, UniverseConfig())
    assert universe.symbols() == ["BTCUSDT"]
    assert universe.excluded["ETHUSDT"] == "delistagem agendada"
    assert universe.excluded["SOLUSDT"] == "histórico curto"


async def test_build_universe_without_delist_endpoint(
    market_api: tuple[FakeBinance, BinanceSpotApi],
) -> None:
    fake, api = market_api
    fake.inject(Fault("GET", "/sapi/v1/spot/delist-schedule", "reject", code=-1000, message="n/d"))
    universe = await build_universe(api, UniverseConfig())
    assert universe.symbols() == ["BTCUSDT", "ETHUSDT"]
