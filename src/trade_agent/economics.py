"""Auditable third-asset commissions and economic PnL, separate from strategy returns."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.errors import BinanceError
from trade_agent.persistence.models import FillRecord
from trade_agent.persistence.store import Store

ZERO = Decimal(0)


@dataclass(frozen=True)
class FeeValuation:
    cost: Decimal
    missing_fill_ids: tuple[int, ...]


async def value_external_fees(
    api: BinanceSpotApi, store: Store, fills: Sequence[FillRecord], *, base: str, quote: str
) -> FeeValuation:
    """Value only commissions not already embedded in base/quote cash flows.

    Use the open of the fill's 1m candle, a historical proxy available at the fill time.
    Persist price, timestamp and source; never substitute a current price or zero for
    missing history. This is a valuation estimate, not an exact contemporaneous tick.
    """
    total = ZERO
    missing: list[int] = []
    for fill in fills:
        if fill.commission_asset in {base, quote} or fill.commission == 0:
            continue
        key = f"commission.quote.{fill.id}"
        saved = await store.get_checkpoint(key)
        if saved is None:
            minute = fill.trade_time // 60_000 * 60_000
            pair = fill.commission_asset + quote
            try:
                candles = await api.klines(
                    pair, "1m", start_time=minute, end_time=minute + 59_999, limit=1
                )
            except BinanceError:
                candles = []
            if not candles or int(candles[0][0]) != minute or Decimal(str(candles[0][1])) <= 0:
                missing.append(fill.id)
                continue
            price = Decimal(str(candles[0][1]))
            saved = {
                "asset": fill.commission_asset,
                "quantity": str(fill.commission),
                "quote": quote,
                "rate": str(price),
                "cost": str(fill.commission * price),
                "fill_time": fill.trade_time,
                "price_time": minute,
                "source": f"binance:{pair}:1m:open",
            }
            await store.set_checkpoint(key, saved)
        if saved["quote"] != quote or saved["quantity"] != str(fill.commission):
            raise ValueError("commission valuation does not match its fill")
        total += Decimal(saved["cost"])
    return FeeValuation(total, tuple(missing))


class OperatingCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: AwareDatetime
    amount_usd: Decimal = Field(ge=0)
    description: str = Field(min_length=1)


@dataclass(frozen=True)
class EconomicPnl:
    trading_pnl_quote: Decimal
    research_usd: Decimal
    operating_usd: Decimal
    economic_pnl_quote: Decimal


def economic_pnl(
    trading_pnl: Decimal,
    research_usd: Decimal,
    costs: Sequence[OperatingCost],
    *,
    since: datetime,
    until: datetime,
    usd_in_quote: Decimal,
) -> EconomicPnl:
    """Expenses use [since, until); USD conversion must be explicit, even for USDT."""
    if (
        since.tzinfo is None
        or until.tzinfo is None
        or until <= since
        or usd_in_quote <= 0
        or research_usd < 0
    ):
        raise ValueError("invalid economic reporting period, conversion or cost")
    operating = sum((c.amount_usd for c in costs if since <= c.at < until), ZERO)
    return EconomicPnl(
        trading_pnl,
        research_usd,
        operating,
        trading_pnl - (research_usd + operating) * usd_in_quote,
    )
