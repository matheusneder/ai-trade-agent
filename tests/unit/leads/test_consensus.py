"""When the followed traders' trades are a signal (D-052), and what a decision cycle does
with it."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.support.leads import NOW, trade
from trade_agent.leads.consensus import (
    BUY,
    NO_DATA,
    SELL,
    SELL_CONSENSUS,
    Backing,
    LeadReading,
    LeadView,
    backings,
)
from trade_agent.leads.source import LeadTrade
from trade_agent.signals import Signal
from trade_agent.signals.local import DOWNTREND, LEAD_CONSENSUS
from trade_agent.strategy.profiles import LeadConsensusConfig

D = Decimal
CONFIG = LeadConsensusConfig()
BALANCE = D(1000)
"""2% of it, the default ``min_buy_pct``, is 20 USDT."""


def _backings(config: LeadConsensusConfig = CONFIG, **leads: list[LeadTrade]) -> dict[str, Backing]:
    readings = {lead: LeadReading(BALANCE, trades) for lead, trades in leads.items()}
    return backings(readings, now=NOW, config=config, quote_asset="USDT")


def test_two_traders_buying_the_same_asset_are_a_buy_consensus() -> None:
    found = _backings(
        ana=[trade("SOLUSDT", quote="100", price="100")],
        bia=[trade("SOLUSDT", quote="300", price="150")],
        caio=[trade("ETHUSDT", quote="50")],
    )
    sol = found["SOLUSDT"]
    assert (sol.buyers, sol.sellers) == (("ana", "bia"), ())
    assert sol.avg_buy_price == D(400) / D(3)  # 1 unit at 100 and 2 at 150
    assert sol.consensus(2) == BUY
    assert found["ETHUSDT"].consensus(2) is None  # one trader alone is no consensus
    assert found["ETHUSDT"].consensus(1) == BUY


def test_a_purchase_counts_net_of_what_the_trader_sold() -> None:
    found = _backings(
        ana=[trade("SOLUSDT", quote="100"), trade("SOLUSDT", buy=False, quote="90")],
        bia=[trade("SOLUSDT", quote="19.99")],  # below 2% of its balance
        caio=[trade("SOLUSDT", quote="20")],  # exactly 2%
    )
    assert found["SOLUSDT"].buyers == ("caio",)


def test_a_round_trip_is_no_purchase_whatever_it_lost() -> None:
    """The review of PR #57: two traders bought 10 SOL for 1,000 and sold the same 10 for 950.
    Netting the quote asset left +50 each, above the 20 of the threshold, and a buy consensus
    behind traders with no position at all. Netted in quantity, a round trip is nothing."""
    round_trip = [
        trade("SOLUSDT", quote="1000", price="100", hours_ago=3),
        trade("SOLUSDT", buy=False, quote="950", price="95", hours_ago=1),
    ]
    assert _backings(ana=round_trip, bia=round_trip) == {}
    # what is left of a position is valued at what it cost; a net sale at what it fetched
    half = [
        trade("SOLUSDT", quote="1000", price="100", hours_ago=3),
        trade("SOLUSDT", buy=False, quote="475", price="95", hours_ago=1),  # 5 of the 10
    ]
    more_sold = [
        trade("SOLUSDT", quote="100", price="100", hours_ago=3),
        trade("SOLUSDT", buy=False, quote="300", price="100", hours_ago=1),
    ]
    found = _backings(ana=half, bia=more_sold)
    assert (found["SOLUSDT"].buyers, found["SOLUSDT"].sellers) == (("ana",), ("bia",))
    assert found["SOLUSDT"].avg_buy_price == D(100)


def test_a_trader_without_a_recent_reading_may_be_a_seller() -> None:
    """The review of PR #57: two buyers and a seller are a margin of one, no consensus; the
    seller's reading going stale must not make it two. Unknown votes count against a buy, and
    a sell consensus (an exit) is the margin among the readings there are."""
    two = Backing("SOLUSDT", buyers=("ana", "bia"), avg_buy_price=D(100))
    assert two.consensus(2) == BUY
    assert two.consensus(2, unknown=1) is None
    assert Backing("SOLUSDT", buyers=("a", "b", "c")).consensus(2, unknown=1) == BUY
    assert Backing("SOLUSDT", sellers=("a", "b")).consensus(2, unknown=3) == SELL
    view = LeadView(selected=3, fresh=2, as_of=NOW, backings={"SOLUSDT": two})
    assert view.unknown == 1
    assert view.consensus("SOLUSDT", CONFIG) is None
    assert view.apply("SOLUSDT", CONFIRMED, CONFIG).setup is None
    assert view.summary(CONFIG)["assets"]["SOLUSDT"]["consensus"] is None  # type: ignore[index]


def test_only_the_window_and_the_accounts_quote_asset_count() -> None:
    found = _backings(
        ana=[trade("SOLUSDT", hours_ago=25)],
        bia=[trade("SOLFDUSD", quote_asset="FDUSD")],
        caio=[trade("SOLUSDT", hours_ago=23.9)],
    )
    assert set(found) == {"SOLUSDT"} and found["SOLUSDT"].buyers == ("caio",)
    week = CONFIG.model_copy(update={"window": timedelta(days=7)})
    assert _backings(week, ana=[trade("SOLUSDT", hours_ago=25)])["SOLUSDT"].buyers == ("ana",)


def test_sellers_cancel_buyers_and_enough_of_them_are_a_sell_consensus() -> None:
    found = _backings(
        ana=[trade("SOLUSDT")],
        bia=[trade("SOLUSDT")],
        caio=[trade("SOLUSDT", buy=False)],
        duda=[trade("ADAUSDT", buy=False), trade("XRPUSDT", buy=False, quote="5")],
        edu=[trade("ADAUSDT", buy=False)],
    )
    assert found["SOLUSDT"].consensus(2) is None  # two buyers, one seller: a margin of one
    assert found["ADAUSDT"] == Backing("ADAUSDT", sellers=("duda", "edu"))
    assert found["ADAUSDT"].consensus(2) == SELL
    assert found["ADAUSDT"].avg_buy_price is None
    assert "XRPUSDT" not in found  # a sale below the threshold


def test_a_trader_backs_only_its_largest_purchases() -> None:
    spray = [trade(f"A{n}USDT", quote=str(100 + n)) for n in range(6)]
    found = _backings(ana=spray, bia=[trade("A0USDT"), trade("A5USDT")])
    assert sorted(found) == ["A0USDT", "A3USDT", "A4USDT", "A5USDT"]
    assert found["A5USDT"].buyers == ("ana", "bia")
    assert found["A0USDT"].buyers == ("bia",)  # the sixth of ana's purchases does not count
    one = CONFIG.model_copy(update={"max_symbols_per_lead": 1})
    assert sorted(_backings(one, ana=spray)) == ["A5USDT"]


def test_a_trader_without_a_balance_backs_nothing() -> None:
    readings = {"ana": LeadReading(D(0), [trade("SOLUSDT")])}
    assert backings(readings, now=NOW, config=CONFIG, quote_asset="USDT") == {}


# ------------------------------------------------------------------ what the cycle reads
CONFIRMED = Signal(score=0.4, setup=LEAD_CONSENSUS, stop_pct=0.03, atr_pct=0.01, close=101.0)
REFUSED = replace(CONFIRMED, setup=None, filtered=DOWNTREND)


def _view(**assets: Backing) -> LeadView:
    return LeadView(selected=4, fresh=4, as_of=NOW, backings=assets)


BOUGHT = Backing("SOLUSDT", buyers=("ana", "bia"), avg_buy_price=D(100))
SOLD = Backing("SOLUSDT", sellers=("ana", "bia"))


def test_without_a_buy_consensus_there_is_no_setup_whatever_the_candles_say() -> None:
    none = _view().apply("SOLUSDT", CONFIRMED, CONFIG)
    assert (none.setup, none.filtered, none.exit_signal) == (None, None, False)
    lone = _view(SOLUSDT=Backing("SOLUSDT", buyers=("ana",), avg_buy_price=D(100)))
    assert lone.apply("SOLUSDT", CONFIRMED, CONFIG).setup is None
    # a pair the candles refused and nobody bought is not worth a line in the report
    assert _view().apply("SOLUSDT", REFUSED, CONFIG).filtered is None


def test_a_confirmed_consensus_is_ranked_by_its_backers_and_capped_above_their_price() -> None:
    signal = _view(SOLUSDT=BOUGHT).apply("SOLUSDT", CONFIRMED, CONFIG)
    assert signal.setup == LEAD_CONSENSUS
    assert signal.score == 0.5  # two of the four followed traders
    assert signal.max_entry_price == pytest.approx(101.5)  # 1.5% above the 100 they paid
    assert (signal.stop_pct, signal.exit_signal) == (CONFIRMED.stop_pct, False)


def test_a_consensus_the_candles_refuse_keeps_the_reason() -> None:
    signal = _view(SOLUSDT=BOUGHT).apply("SOLUSDT", REFUSED, CONFIG)
    assert (signal.setup, signal.filtered) == (None, DOWNTREND)
    assert signal.max_entry_price is None


def test_a_sell_consensus_is_an_exit_and_names_itself() -> None:
    view = _view(SOLUSDT=SOLD)
    signal = view.apply("SOLUSDT", CONFIRMED, CONFIG)
    assert (signal.setup, signal.exit_signal) == (None, True)
    assert view.sold("SOLUSDT", CONFIG) and not view.sold("ETHUSDT", CONFIG)
    assert view.exit_reason("SOLUSDT", "saída técnica", CONFIG) == SELL_CONSENSUS
    assert view.exit_reason("ETHUSDT", "saída técnica", CONFIG) == "saída técnica"
    assert view.exit_reason("ETHUSDT", None, CONFIG) is None
    # the family's own exit stays an exit without any consensus
    falling = replace(REFUSED, exit_signal=True)
    assert _view().apply("SOLUSDT", falling, CONFIG).exit_signal


def test_without_a_reading_nothing_is_bought_or_sold_on_the_traders() -> None:
    blind = LeadView(NO_DATA, selected=4, backings={"SOLUSDT": BOUGHT, "ADAUSDT": SOLD})
    signal = blind.apply("SOLUSDT", CONFIRMED, CONFIG)
    assert (signal.setup, signal.filtered, signal.exit_signal) == (None, None, False)
    assert not blind.sold("ADAUSDT", CONFIG)


def test_a_consensus_without_a_price_keeps_the_candles_signal() -> None:
    priceless = _view(SOLUSDT=Backing("SOLUSDT", buyers=("ana", "bia")))
    assert priceless.apply("SOLUSDT", CONFIRMED, CONFIG) == CONFIRMED


def test_the_summary_says_who_was_behind_each_asset() -> None:
    view = _view(SOLUSDT=BOUGHT, ADAUSDT=replace(SOLD, symbol="ADAUSDT"))
    assert view.summary(CONFIG) == {
        "status": "ok",
        "selected": 4,
        "fresh": 4,
        "as_of": NOW.isoformat(),
        "assets": {
            "SOLUSDT": {
                "buyers": ["ana", "bia"],
                "sellers": [],
                "avg_buy_price": "100",
                "consensus": "buy",
            },
            "ADAUSDT": {
                "buyers": [],
                "sellers": ["ana", "bia"],
                "avg_buy_price": None,
                "consensus": "sell",
            },
        },
    }
    empty = LeadView(NO_DATA, selected=2).summary(CONFIG)
    assert (empty["status"], empty["as_of"], empty["assets"]) == (NO_DATA, None, {})
