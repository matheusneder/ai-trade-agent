from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from trade_agent.economics import OperatingCost, economic_pnl, value_external_fees
from trade_agent.exchange.errors import BinanceConnectionError
from trade_agent.persistence.models import FillRecord

D = Decimal
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def test_economic_pnl_expenses_and_exclusive_boundary() -> None:
    costs = [
        OperatingCost(at=NOW, amount_usd=D(3), description="hosting"),
        OperatingCost(at=NOW + timedelta(days=1), amount_usd=D(99), description="next day"),
    ]
    result = economic_pnl(
        D(10), D(2), costs, since=NOW, until=NOW + timedelta(days=1), usd_in_quote=D("1.1")
    )
    assert result.operating_usd == 3
    assert result.economic_pnl_quote == D("4.5")
    with pytest.raises(ValueError, match="invalid"):
        economic_pnl(D(0), D(0), [], since=NOW, until=NOW, usd_in_quote=D(1))
    with pytest.raises(ValidationError):
        OperatingCost(at=datetime(2026, 10, 20), amount_usd=D(1), description="naive")  # noqa: DTZ001


async def test_external_fee_uses_historical_price_and_reuses_audit() -> None:
    api, store = AsyncMock(), AsyncMock()
    fill = FillRecord(id=7, commission_asset="BNB", commission=D(".01"), trade_time=125_000)
    store.get_checkpoint.return_value = None
    api.klines.return_value = [[120_000, "600"]]
    first = await value_external_fees(api, store, [fill], base="BTC", quote="USDT")
    assert first.cost == 6 and first.missing_fill_ids == ()
    api.klines.assert_awaited_once_with(
        "BNBUSDT", "1m", start_time=120_000, end_time=179_999, limit=1
    )
    saved = store.set_checkpoint.call_args.args[1]
    assert saved["price_time"] <= fill.trade_time
    store.get_checkpoint.return_value = saved
    assert await value_external_fees(api, store, [fill], base="BTC", quote="USDT") == first
    assert api.klines.await_count == 1


async def test_missing_commission_history_is_explicit_and_base_quote_not_double_counted() -> None:
    api, store = AsyncMock(), AsyncMock()
    store.get_checkpoint.return_value = None
    api.klines.return_value = [[180_000, "600"]]  # a future price is not an acceptable proxy
    fills = [
        FillRecord(id=n, commission_asset=asset, commission=D(".01"), trade_time=125_000)
        for n, asset in enumerate(["BTC", "USDT", "BNB"])
    ]
    result = await value_external_fees(api, store, fills, base="BTC", quote="USDT")
    assert result.cost == 0 and result.missing_fill_ids == (2,)
    store.set_checkpoint.assert_not_awaited()
    api.klines.side_effect = BinanceConnectionError("unavailable")
    assert await value_external_fees(api, store, fills, base="BTC", quote="USDT") == result
    store.get_checkpoint.return_value = {"quote": "OTHER", "quantity": ".01"}
    with pytest.raises(ValueError, match="does not match"):
        await value_external_fees(api, store, fills, base="BTC", quote="USDT")
